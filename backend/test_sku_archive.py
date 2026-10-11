"""SKU lifecycle regressions, with foreign keys enabled in an isolated database."""
import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ADMIN"] = "false"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import database
from database import DEFAULT_STORES, get_session
from models import (AuditEvent, Inventory, InventoryCount, InventoryCountItem, ManagementOperation,
                    Product, SalesOrder, SalesOrderItem, SalesRefund, SalesRefundItem, Sku,
                    StockLog, StockTransfer, StockTransferBatch, StockTransferItem, User)
from routers import orders, products, skus, stock
from security import get_current_user
from time_utils import utc_iso, utc_now


class SkuArchiveTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda db, _: db.execute("PRAGMA foreign_keys=ON"))
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add_all([User(id=1, username="manager", role="manager", password_hash="unused"),
                             User(id=2, username="staff", role="staff", store=DEFAULT_STORES[0], password_hash="unused"),
                             User(id=3, username="other", role="manager", password_hash="unused"),
                             Product(id=1, code="OLD", name="Old shirt")])
            session.flush()
            session.add_all([Sku(id=i, product_id=1, barcode=f"OLD-{i}", color="Blue", size=str(i),
                                 price=100, cost=40, quantity=999) for i in (1, 2)])
            session.flush()
            session.add_all([Inventory(sku_id=i, store=store, quantity=0)
                             for i in (1, 2) for store in DEFAULT_STORES])
            session.commit()
        app = FastAPI()
        for router in (skus.router, stock.router, orders.router, products.router):
            app.include_router(router)
        def session_override():
            with Session(self.engine) as session:
                yield session
        self.actor_id = 1
        def actor():
            with Session(self.engine) as session:
                return session.get(User, self.actor_id)
        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_user] = actor
        self.client = TestClient(app)
        self.serial = 0

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def status(self, sku_id=1):
        response = self.client.get(f"/api/skus/{sku_id}/lifecycle")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def payload(self, sku_id=1, version=None):
        self.serial += 1
        return {"client_request_id": f"sku-lifecycle-test-{self.serial:05d}",
                "expected_version": self.status(sku_id)["version"] if version is None else version}

    def archive(self, sku_id=1, data=None):
        return self.client.post(f"/api/skus/{sku_id}/archive", json=data or self.payload(sku_id))

    def quantity(self, value, sku_id=1, store=DEFAULT_STORES[0]):
        with Session(self.engine) as session:
            inv = session.exec(select(Inventory).where(Inventory.sku_id == sku_id, Inventory.store == store)).one()
            inv.quantity = value
            inv.updated_at = utc_now()
            session.add(inv)
            session.commit()

    def sale(self):
        self.quantity(1)
        data = {"client_request_id": "archive-sale-test-00001", "store": DEFAULT_STORES[0], "items": [{"sku_id": 1, "qty": 1}]}
        response = self.client.post("/api/sales", json=data)
        self.assertEqual(response.status_code, 200, response.text)
        with Session(self.engine) as session:
            order_id = session.exec(select(SalesOrder.id).where(SalesOrder.client_request_id == data["client_request_id"])).one()
        return order_id, data

    def count(self, status="draft", sku_id=1):
        with Session(self.engine) as session:
            count = InventoryCount(count_no="COUNT-TEST", store=DEFAULT_STORES[0], status=status, created_by="manager")
            session.add(count)
            session.flush()
            session.add(InventoryCountItem(count_id=count.id, sku_id=sku_id, system_qty=0, actual_qty=0, inventory_updated_at=utc_now()))
            session.commit()
            return count.id

    def test_archive_hides_from_default_and_inactive_lists_without_removing_rows(self):
        response = self.archive()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["active"])
        self.assertTrue(response.json()["archived_at"])
        for suffix in ("", "?include_inactive=true", "?include_inactive=true&store=all"):
            self.assertEqual([row["id"] for row in self.client.get("/api/skus" + suffix).json()], [2])
        archived = self.client.get("/api/skus/archived?offset=0&limit=1").json()
        self.assertEqual((archived["total"], archived["items"][0]["barcode"]), (1, "OLD-1"))
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(Inventory)).all()), 8)
            self.assertEqual(session.get(Sku, 1).version, 1)
            self.assertEqual(session.exec(select(AuditEvent.action)).all(), ["sku.archive"])

    def test_each_store_inventory_blocks_archive_with_clear_reason(self):
        for store in DEFAULT_STORES:
            with self.subTest(store=store):
                self.quantity(1, store=store)
                status = self.status()
                self.assertFalse(status["can_archive"])
                response = self.archive()
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(store, response.json()["detail"])
                self.quantity(0, store=store)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(ManagementOperation)).all(), [])
            self.assertEqual(session.exec(select(AuditEvent)).all(), [])

    def test_nonzero_unknown_store_is_not_ignored(self):
        with Session(self.engine) as session:
            session.add(Inventory(sku_id=1, store="legacy", quantity=1))
            session.commit()
        self.assertEqual(self.archive().status_code, 400)

    def test_single_shipped_transfer_blocks_archive_but_received_history_allows_it(self):
        with Session(self.engine) as session:
            transfer = StockTransfer(sku_id=1, transfer_no="TRANSFER-1", qty=1, from_store=DEFAULT_STORES[0], to_store=DEFAULT_STORES[1], created_by="manager")
            session.add(transfer)
            session.commit()
            transfer_id = transfer.id
        self.assertEqual(self.archive().status_code, 400)
        with Session(self.engine) as session:
            transfer = session.get(StockTransfer, transfer_id)
            transfer.status = "received"
            session.add(transfer)
            session.commit()
        self.assertFalse(self.status()["can_delete"])
        self.assertEqual(self.archive().status_code, 200)

    def test_partial_batch_receipt_blocks_only_skus_still_in_transit(self):
        with Session(self.engine) as session:
            batch = StockTransferBatch(transfer_no="BATCH-1", status="partial", from_store=DEFAULT_STORES[0], to_store=DEFAULT_STORES[1], created_by="manager")
            session.add(batch)
            session.flush()
            session.add_all([StockTransferItem(transfer_id=batch.id, sku_id=1, qty=2, received_qty=1, rejected_qty=1),
                             StockTransferItem(transfer_id=batch.id, sku_id=2, qty=2, received_qty=1)])
            session.commit()
        self.assertEqual(self.archive(2).status_code, 400)
        self.assertEqual(self.archive(1).status_code, 200)

    def test_draft_count_blocks_archive_and_cancelled_count_only_blocks_delete(self):
        count_id = self.count()
        self.assertEqual(self.archive().status_code, 400)
        self.assertIn("COUNT-TEST", " ".join(self.status()["archive_reasons"]))
        response = self.client.post(f"/api/stock/counts/{count_id}/cancel", json={"expected_version": 0})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.status()["can_delete"])
        self.assertEqual(self.archive().status_code, 200)

    def test_completed_counts_keep_history_and_do_not_block_archive(self):
        count_id = self.count("completed")
        self.assertEqual(self.archive().status_code, 200)
        self.assertEqual(self.client.get(f"/api/stock/counts/{count_id}").json()["items"][0]["barcode"], "OLD-1")

    def test_archived_sku_is_excluded_from_new_count_sheets(self):
        self.archive()
        response = self.client.post("/api/stock/counts", json={"store": DEFAULT_STORES[0]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["sku_id"] for row in response.json()["count"]["items"]], [2])

    def test_sales_and_stock_history_remain_readable(self):
        order_id, sale = self.sale()
        before = self.client.get(f"/api/orders/{order_id}").json()
        self.assertEqual(self.archive().status_code, 200)
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").json(), before)
        self.assertEqual(self.client.post("/api/sales", json=sale).status_code, 200)
        self.assertEqual(self.client.delete("/api/skus/1").status_code, 400)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(StockLog)).all()), 1)
            self.assertEqual(len(session.exec(select(SalesOrderItem)).all()), 1)

    def test_restore_remains_inactive_with_same_barcode_and_prices(self):
        self.archive()
        response = self.client.post("/api/skus/1/restore", json=self.payload())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["active"])
        self.assertIsNone(response.json()["archived_at"])
        self.assertEqual([row["id"] for row in self.client.get("/api/skus").json()], [2])
        self.assertEqual(len(self.client.get("/api/skus?include_inactive=true").json()), 2)
        sku = self.client.get("/api/skus/1").json()
        self.assertEqual((sku["barcode"], sku["price"], sku["cost"], sku["quantity"]), ("OLD-1", 100, 40, 0))
        self.assertEqual(self.client.get("/api/skus/by-barcode/OLD-1").status_code, 400)
        self.assertEqual(self.client.put("/api/skus/1", json={"active": True, "expected_version": 2}).status_code, 200)

    def test_archive_and_restore_retries_return_original_receipts_after_later_changes(self):
        data = self.payload()
        first = self.archive(data=data).json()
        restore_data = self.payload()
        restored = self.client.post("/api/skus/1/restore", json=restore_data).json()
        self.assertEqual(self.archive(data=data).json(), first)
        self.assertEqual(self.client.post("/api/skus/1/restore", json=restore_data).json(), restored)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(Sku, 1).archived_at)
            self.assertEqual(session.get(Sku, 1).version, 2)
            self.assertEqual(len(session.exec(select(AuditEvent)).all()), 2)

    def test_retry_cannot_change_owner_action_sku_or_payload(self):
        data = self.payload()
        self.archive(data=data)
        for url, body in (("/api/skus/1/archive", {**data, "note": "changed"}),
                          ("/api/skus/2/archive", data), ("/api/skus/1/restore", data)):
            self.assertEqual(self.client.post(url, json=body).status_code, 409)
        self.actor_id = 3
        self.assertEqual(self.archive(data=data).status_code, 409)

    def test_stale_version_and_repeat_new_operation_do_not_change_state(self):
        old = self.payload()
        self.client.put("/api/skus/1", json={"cost": 41, "expected_version": 0})
        self.assertEqual(self.archive(data=old).status_code, 409)
        self.assertEqual(self.archive().status_code, 200)
        self.assertEqual(self.archive().status_code, 400)
        self.assertEqual(self.client.post("/api/skus/2/restore", json=self.payload(2)).status_code, 409)

    def test_archive_rolls_back_state_audit_and_receipt_if_commit_fails(self):
        data = self.payload()
        with patch("routers.skus.finish_operation", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                self.archive(data=data)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(Sku, 1).archived_at)
            self.assertTrue(session.get(Sku, 1).active)
            self.assertEqual(session.exec(select(AuditEvent)).all(), [])
            self.assertEqual(session.exec(select(ManagementOperation)).all(), [])
        self.assertEqual(self.archive(data=data).status_code, 200)

    def test_archive_blocks_enable_prices_sales_stock_corrections_and_transfers(self):
        snapshot = self.client.get("/api/skus/1").json()
        self.archive()
        requests = [
            ("put", "/api/skus/1", {"active": True}),
            ("put", "/api/skus/1", {"cost": 50}),
            ("post", "/api/skus/pricing/batch", {"client_request_id": "archive-pricing-00001", "product_id": 1, "items": [{"sku_id": 2, "expected_version": 0}, {"sku_id": 1, "expected_version": 1}], "cost": 90}),
            ("post", "/api/sales", {"client_request_id": "archived-new-sale-001", "items": [{"sku_id": 1, "qty": 1}]}),
            ("post", "/api/stock/in", {"client_request_id": "archived-stock-in-001", "sku_id": 1, "qty": 1}),
            ("post", "/api/stock/out", {"client_request_id": "archived-stock-out-01", "sku_id": 1, "qty": 1}),
            ("post", "/api/stock/corrections", {"client_request_id": "archived-correct-0001", "sku_id": 1, "store": DEFAULT_STORES[0], "expected_quantity": 0, "expected_updated_at": snapshot["inventory_updated_at"], "actual_qty": 1, "note": "test"}),
            ("post", "/api/stock/transfers", {"client_request_id": "archived-transfer-001", "sku_id": 1, "qty": 1, "from_store": DEFAULT_STORES[0], "to_store": DEFAULT_STORES[1]}),
            ("post", "/api/stock/transfer-batches", {"client_request_id": "archived-batch-000001", "items": [{"sku_id": 1, "qty": 1}], "from_store": DEFAULT_STORES[0], "to_store": DEFAULT_STORES[1]}),
        ]
        for method, url, body in requests:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url, json=body)
                self.assertIn(response.status_code, (400, 409), response.text)
        self.assertEqual(self.client.get("/api/skus/by-barcode/OLD-1").status_code, 400)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Sku, 2).cost, 40)
            self.assertEqual(session.exec(select(StockLog)).all(), [])

    def test_historical_refund_restores_archived_sku_inactive_and_retries_once(self):
        order_id, _ = self.sale()
        self.archive()
        data = {"client_request_id": "archived-refund-00001", "store": DEFAULT_STORES[3], "items": [{"sku_id": 1, "qty": 1}]}
        response = self.client.post(f"/api/orders/{order_id}/refund", json=data)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/refund", json=data).status_code, 200)
        with Session(self.engine) as session:
            sku = session.get(Sku, 1)
            self.assertEqual((sku.active, sku.archived_at, sku.version), (False, None, 2))
            self.assertEqual(sum(row.quantity for row in session.exec(select(Inventory).where(Inventory.sku_id == 1)).all()), 1)
            self.assertEqual(len(session.exec(select(SalesRefund)).all()), 1)
            self.assertEqual(len(session.exec(select(SalesRefundItem)).all()), 1)
            self.assertEqual(session.exec(select(AuditEvent.action).where(AuditEvent.action == "sku.restore")).all(), ["sku.restore"])
        self.assertEqual(self.client.get("/api/skus/by-barcode/OLD-1").status_code, 400)

    def test_invalid_refund_does_not_restore_archive_or_change_inventory(self):
        order_id, _ = self.sale()
        self.archive()
        response = self.client.post(f"/api/orders/{order_id}/refund", json={"client_request_id": "bad-archive-refund-01", "items": [{"sku_id": 1, "qty": 2}]})
        self.assertEqual(response.status_code, 400, response.text)
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(Sku, 1).archived_at)
            self.assertEqual(session.exec(select(SalesRefund)).all(), [])
            self.assertEqual(session.exec(select(AuditEvent.action).where(AuditEvent.action == "sku.restore")).all(), [])

    def test_archived_barcode_and_variant_remain_reserved(self):
        self.archive()
        for body in ({"color": "Blue", "size": "1"}, {"color": "New", "size": "M", "barcode": "OLD-1"}):
            self.assertEqual(self.client.post("/api/products/1/skus", json=body).status_code, 400)

    def test_manager_only_lifecycle_and_archive_access(self):
        self.actor_id = 2
        for url in ("/api/skus/archived", "/api/skus/1/lifecycle"):
            self.assertEqual(self.client.get(url).status_code, 403)
        for action in ("archive", "restore"):
            self.assertEqual(self.client.post(f"/api/skus/1/{action}", json=self.payload(version=0)).status_code, 403)
        self.assertEqual(self.client.delete("/api/skus/1").status_code, 403)

    def test_pagination_keyword_and_limits(self):
        self.archive(1)
        self.archive(2)
        for offset in (0, 1):
            response = self.client.get(f"/api/skus/archived?limit=1&offset={offset}").json()
            self.assertEqual(response["total"], 2)
            self.assertEqual(len(response["items"]), 1)
        response = self.client.get("/api/skus/archived?keyword=OLD-1").json()
        self.assertEqual((response["total"], response["items"][0]["id"]), (1, 1))
        self.assertEqual(self.client.get("/api/skus/archived?keyword=missing").json()["items"], [])
        for query in ("limit=0", "limit=101", "offset=-1"):
            self.assertEqual(self.client.get("/api/skus/archived?" + query).status_code, 422)

    def test_physical_delete_only_unused_sku_and_retry_is_durable(self):
        data = self.payload()
        response = self.client.request("DELETE", "/api/skus/1", json=data)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.request("DELETE", "/api/skus/1", json=data).json(), response.json())
        with Session(self.engine) as session:
            self.assertIsNone(session.get(Sku, 1))
            self.assertEqual(len(session.exec(select(Inventory)).all()), 4)

    def test_count_reference_prevents_delete_without_stocklog(self):
        self.count("cancelled")
        response = self.client.delete("/api/skus/1")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("盘点记录", response.json()["detail"])

    def test_product_delete_cannot_bypass_archive_or_count_restrictions(self):
        self.archive(1)
        self.assertEqual(self.client.delete("/api/products/1").status_code, 400)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(Sku)).all()), 2)
            self.assertEqual(len(session.exec(select(Inventory)).all()), 8)

    def test_sku_locks_compile_for_postgresql_before_inventory_writes(self):
        statements = []
        def capture(conn, cursor, sql, params, context, many):
            stmt = context.compiled.statement if context.compiled else None
            if stmt is not None and getattr(stmt, "_for_update_arg", None) is not None:
                statements.append(str(stmt.compile(dialect=postgresql.dialect())))
        event.listen(self.engine, "before_cursor_execute", capture)
        self.archive()
        self.assertIn("FOR UPDATE", statements[0])
        self.assertIn("ORDER BY sku.id", statements[0])
        self.assertIn("inventory", statements[1])

    def test_existing_database_migration_is_repeatable_and_preserves_business_data(self):
        with self.engine.begin() as conn:
            conn.execute(text("drop index ix_sku_archived_at"))
            conn.execute(text("alter table sku drop column archived_at"))
        with patch.object(database, "engine", self.engine):
            database._ensure_runtime_schema()
            database._ensure_runtime_schema()
            database._ensure_store_inventory_rows()
        with Session(self.engine) as session:
            self.assertEqual(session.get(Sku, 1).quantity, 999)
            self.assertIsNone(session.get(Sku, 1).archived_at)
            self.assertEqual(len(session.exec(select(Inventory)).all()), 8)
            self.assertTrue(all(row.quantity == 0 for row in session.exec(select(Inventory)).all()))


if __name__ == "__main__":
    unittest.main()

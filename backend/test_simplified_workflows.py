"""Transactional management workflows, using a disposable database only."""
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ADMIN"] = "false"

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlmodel import Session, SQLModel, create_engine, select

from database import DEFAULT_STORES, get_session
from models import AuditEvent, Inventory, InventoryCount, ManagementOperation, Product, SalesOrder, SalesOrderItem, Sku, StockLog, User
from routers import products, skus, stock
from schemas import InventoryCorrection, InventoryCountCreate, InventoryCountComplete, ProductIntake, SkuBatchPricing, SkuUpdate
from security import get_current_user
from time_utils import utc_iso


class SimplifiedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add_all([User(id=1, username="manager", role="manager", password_hash="unused"),
                             User(id=2, username="staff", role="staff", store=DEFAULT_STORES[0], password_hash="unused"),
                             User(id=3, username="other", role="manager", password_hash="unused"),
                             Product(id=1, code="OLD", name="Old shirt"),
                             Product(id=2, code="OTHER", name="Other shirt")])
            session.flush()
            session.add_all([Sku(id=i, product_id=1 if i<4 else 2, barcode=f"OLD-{i}",
                                 color="Blue", size=str(i), price=100+i, cost=40+i, active=i!=3)
                             for i in range(1,5)])
            session.flush()
            session.add_all([Inventory(sku_id=i, store=store, quantity=8)
                             for i in range(1,5) for store in DEFAULT_STORES])
            session.commit()
        self.app = FastAPI()
        for router in (products.router, skus.router, stock.router):
            self.app.include_router(router)
        def session_override():
            with Session(self.engine) as session:
                yield session
        self.app.dependency_overrides[get_session] = session_override
        self.user_id = 1
        def actor():
            with Session(self.engine) as session:
                return session.get(User, self.user_id)
        self.app.dependency_overrides[get_current_user] = actor
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def pricing(self, **extra):
        return {"client_request_id":"batch-pricing-00001", "product_id":1,
                "items":[{"sku_id":i,"expected_version":0} for i in (1,2)], "cost":50, **extra}

    def intake(self, **extra):
        return {"client_request_id":"product-intake-00001", "product":{"code":"NEW", "name":"New shirt", "category":"T恤衫", "tag_price":199},
                "store":DEFAULT_STORES[1], "variants":[
                    {"color":"White", "size":"M", "price":129, "cost":60, "quantity":3},
                    {"color":"White", "size":"L", "price":139, "cost":65, "quantity":0}], **extra}

    def correction(self, **extra):
        with Session(self.engine) as session:
            inv = session.exec(select(Inventory).where(Inventory.sku_id==1, Inventory.store==DEFAULT_STORES[0])).one()
            return {"client_request_id":"inventory-correct-00001", "sku_id":1, "store":inv.store,
                    "expected_quantity":inv.quantity, "expected_updated_at":utc_iso(inv.updated_at),
                    "actual_qty":2, "note":"Counted after entry mistake", **extra}

    def assert_no_operations(self):
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(ManagementOperation)).all(), [])
            self.assertEqual(session.exec(select(AuditEvent)).all(), [])
            self.assertEqual(session.exec(select(StockLog)).all(), [])

    def test_cost_only_prices_inventory_and_unselected_variants_unchanged(self):
        response = self.client.post("/api/skus/pricing/batch", json=self.pricing())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["changed_count"], 2)
        with Session(self.engine) as session:
            for i in range(1,5):
                sku = session.get(Sku,i)
                self.assertEqual(sku.price,100+i)
                self.assertEqual(sku.cost,50 if i<3 else 40+i)
                self.assertEqual(sku.version,1 if i<3 else 0)
                self.assertEqual(sku.barcode,f"OLD-{i}")
            self.assertTrue(all(inv.quantity==8 for inv in session.exec(select(Inventory)).all()))
            self.assertEqual(len(session.exec(select(AuditEvent)).all()),2)
            self.assertEqual(session.exec(select(StockLog)).all(),[])

    def test_price_only_and_inactive_variant_are_supported(self):
        data = self.pricing(items=[{"sku_id":3,"expected_version":0}], price=0)
        del data["cost"]
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=data).status_code,200)
        with Session(self.engine) as session:
            sku = session.get(Sku,3)
            self.assertEqual((sku.price,sku.cost,sku.active),(0,43,False))

    def test_all_versions_checked_before_any_price_is_saved(self):
        data = self.pricing(items=[{"sku_id":1,"expected_version":0},{"sku_id":2,"expected_version":1}])
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=data).status_code,409)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Sku,1).cost,41)
        self.assert_no_operations()

    def test_invalid_selection_cannot_touch_other_products(self):
        for ids in [(1,1),(1,4),(1,999)]:
            with self.subTest(ids=ids):
                data = self.pricing(items=[{"sku_id":i,"expected_version":0} for i in ids])
                self.assertIn(self.client.post("/api/skus/pricing/batch",json=data).status_code,(400,409))
                self.assert_no_operations()

    def test_pricing_idempotency_replays_original_result_even_after_later_edit(self):
        first = self.client.post("/api/skus/pricing/batch",json=self.pricing()).json()
        with Session(self.engine) as session:
            skus.update_sku(1,SkuUpdate(cost=80,expected_version=1),session.get(User,1),session)
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=self.pricing()).json(),first)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Sku,1).cost,80)
            self.assertEqual(len(session.exec(select(ManagementOperation)).all()),1)
            self.assertEqual(len(session.exec(select(AuditEvent)).all()),3)

    def test_receipt_cannot_be_reused_by_other_owner_or_changed_payload(self):
        self.client.post("/api/skus/pricing/batch",json=self.pricing())
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=self.pricing(cost=51)).status_code,409)
        self.user_id = 3
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=self.pricing()).status_code,409)

    def test_noop_is_receipted_without_increment_or_audit(self):
        data = self.pricing(items=[{"sku_id":1,"expected_version":0}],cost=41)
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=data).json()["changed_count"],0)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Sku,1).version,0)
            self.assertEqual(len(session.exec(select(ManagementOperation)).all()),1)
            self.assertEqual(session.exec(select(AuditEvent)).all(),[])

    def test_pricing_rollback_includes_receipt_and_partial_update(self):
        with Session(self.engine) as session:
            with patch("routers.skus.add_audit_event",side_effect=RuntimeError("audit failed")):
                with self.assertRaises(RuntimeError):
                    skus.batch_update_pricing(SkuBatchPricing(**self.pricing()),session.get(User,1),session)
            self.assertEqual(session.get(Sku,1).cost,41)
        self.assert_no_operations()
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=self.pricing()).status_code,200)

    def test_intake_creates_all_variants_and_only_target_store_stock(self):
        response = self.client.post("/api/products/intake",json=self.intake())
        self.assertEqual(response.status_code,200,response.text)
        result = response.json()
        self.assertEqual(result["created_count"],2)
        self.assertEqual(len(set(item["barcode"] for item in result["items"])),2)
        with Session(self.engine) as session:
            product = session.get(Product,result["product_id"])
            self.assertFalse(product.label_verified)
            self.assertIsNone(product.composition)
            for item in result["items"]:
                invs = session.exec(select(Inventory).where(Inventory.sku_id==item["id"])).all()
                self.assertEqual(len(invs),4)
                for inv in invs:
                    self.assertEqual(inv.quantity,item["quantity"] if inv.store==DEFAULT_STORES[1] else 0)
            self.assertEqual(len(session.exec(select(StockLog)).all()),1)
            self.assertEqual(len(session.exec(select(AuditEvent)).all()),1)

    def test_intake_retry_does_not_duplicate_variants_or_initial_stock(self):
        first = self.client.post("/api/products/intake",json=self.intake()).json()
        self.assertEqual(self.client.post("/api/products/intake",json=self.intake()).json(),first)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(Sku)).all()),6)
            self.assertEqual(len(session.exec(select(StockLog)).all()),1)

    def test_intake_existing_code_and_duplicate_pairs_leave_no_partial_records(self):
        for data in [self.intake(product={"code":"OLD","name":"Overwrite"}),
                     self.intake(variants=[self.intake()["variants"][0]]*2)]:
            with self.subTest(data=data):
                self.assertIn(self.client.post("/api/products/intake",json=data).status_code,(400,409))
                self.assert_no_operations()
                with Session(self.engine) as session:
                    self.assertEqual(len(session.exec(select(Product)).all()),2)

    def test_intake_validation_and_label_verification(self):
        for change in [{"store":"all"},{"product":{"code":" ","name":" "}},
                       {"label":{"label_verified":True}}]:
            self.assertIn(self.client.post("/api/products/intake",json=self.intake(**change)).status_code,(400,422))
            self.assert_no_operations()
        data = self.intake(label={"composition":"100%棉","execution_standard":"GB/T 22849-2024",
                                "label_usage":"adult_skin","safety_category":"B","label_verified":True})
        response = self.client.post("/api/products/intake",json=data)
        self.assertEqual(response.status_code,200,response.text)
        with Session(self.engine) as session:
            self.assertTrue(session.get(Product,response.json()["product_id"]).label_verified)

    def test_intake_rolls_back_all_rows_if_second_variant_fails(self):
        with Session(self.engine) as session:
            with patch("routers.products._next_unique_barcode",side_effect=["NEW01",RuntimeError("failure")]):
                with self.assertRaises(RuntimeError):
                    products.intake_product(ProductIntake(**self.intake()),session.get(User,1),session)
            self.assertEqual(len(session.exec(select(Product)).all()),2)
            self.assertEqual(len(session.exec(select(Sku)).all()),4)
            self.assertEqual(len(session.exec(select(Inventory)).all()),16)
        self.assert_no_operations()

    def test_correction_changes_only_selected_store_and_creates_trace(self):
        data = self.correction(actual_qty=0)
        response = self.client.post("/api/stock/corrections",json=data)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["change"],-8)
        with Session(self.engine) as session:
            for inv in session.exec(select(Inventory)).all():
                self.assertEqual(inv.quantity,0 if inv.sku_id==1 and inv.store==DEFAULT_STORES[0] else 8)
            self.assertEqual((session.get(Sku,1).price,session.get(Sku,1).cost,session.get(Sku,1).barcode),(101,41,"OLD-1"))
            log = session.exec(select(StockLog)).one()
            self.assertEqual((log.type,log.change,log.note,log.operator_user_id),("adjust",-8,data["note"],1))
            self.assertEqual(session.exec(select(AuditEvent)).one().action,"stock.correction")

    def test_correction_stale_quantity_or_aba_timestamp_never_overwrites(self):
        data = self.correction()
        with Session(self.engine) as session:
            inv = session.exec(select(Inventory).where(Inventory.sku_id==1,Inventory.store==DEFAULT_STORES[0])).one()
            inv.updated_at += timedelta(seconds=1)
            session.add(inv); session.commit()
        self.assertEqual(self.client.post("/api/stock/corrections",json=data).status_code,409)
        self.assert_no_operations()
        self.assertEqual(self.client.post("/api/stock/corrections",json=self.correction(expected_quantity=9)).status_code,409)
        self.assert_no_operations()

    def test_correction_retry_keeps_later_stock_movements(self):
        data = self.correction()
        first = self.client.post("/api/stock/corrections",json=data).json()
        with Session(self.engine) as session:
            stock._adjust_stock(session,1,DEFAULT_STORES[0],3); session.commit()
        self.assertEqual(self.client.post("/api/stock/corrections",json=data).json(),first)
        with Session(self.engine) as session:
            self.assertEqual(stock._current_qty(session,1,DEFAULT_STORES[0]),5)
            self.assertEqual(len(session.exec(select(StockLog)).all()),1)

    def test_correction_rollback_when_audit_fails(self):
        with Session(self.engine) as session:
            with patch("routers.stock.add_audit_event",side_effect=RuntimeError("audit failed")):
                with self.assertRaises(RuntimeError):
                    stock.correct_inventory(InventoryCorrection(**self.correction()),session.get(User,1),session)
            self.assertEqual(stock._current_qty(session,1,DEFAULT_STORES[0]),8)
        self.assert_no_operations()

    def test_zero_delta_and_inactive_inventory_corrections_are_traceable(self):
        data = self.correction(sku_id=3,actual_qty=8)
        with Session(self.engine) as session:
            data["expected_updated_at"] = utc_iso(session.exec(select(Inventory).where(Inventory.sku_id==3,Inventory.store==DEFAULT_STORES[0])).one().updated_at)
        response = self.client.post("/api/stock/corrections",json=data)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["change"],0)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(StockLog)).one().change,0)

    def test_staff_cannot_mutate_any_new_management_endpoint(self):
        self.user_id = 2
        for path,data in [("/api/skus/pricing/batch",self.pricing()),("/api/products/intake",self.intake()),("/api/stock/corrections",self.correction())]:
            self.assertEqual(self.client.post(path,json=data).status_code,403)
        self.assert_no_operations()

    def test_invalid_money_counts_and_empty_reason_rejected(self):
        for cost in (None,-1,0.001):
            self.assertEqual(self.client.post("/api/skus/pricing/batch",json=self.pricing(cost=cost)).status_code,422)
        for change in ({"actual_qty":-1},{"actual_qty":1.5},{"actual_qty":2147483648},{"note":" "}):
            self.assertEqual(self.client.post("/api/stock/corrections",json=self.correction(**change)).status_code,422)
        for value in (float("nan"),float("inf"),0.001):
            with self.assertRaises(ValidationError):
                SkuBatchPricing(**self.pricing(cost=value))
        self.assert_no_operations()

    def test_postgresql_statements_keep_lock_order_and_conditional_snapshot_update(self):
        statements=[]
        def capture(conn, clause, multiparams, params, options):
            statements.append(str(clause.compile(dialect=postgresql.dialect())))
        event.listen(self.engine,"before_execute",capture)
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=self.pricing()).status_code,200)
        self.assertTrue(any("ORDER BY sku.id FOR UPDATE" in sql for sql in statements))
        self.assertTrue(any("UPDATE sku SET" in sql and "sku.version =" in sql for sql in statements))
        statements.clear()
        self.assertEqual(self.client.post("/api/stock/corrections",json=self.correction()).status_code,200)
        self.assertTrue(any("FROM inventory" in sql and "FOR UPDATE" in sql for sql in statements))
        self.assertTrue(any("UPDATE inventory SET" in sql and "inventory.updated_at =" in sql and "inventory.quantity =" in sql for sql in statements))

    def test_existing_count_sheet_cannot_overwrite_a_new_correction(self):
        with Session(self.engine) as session:
            sheet=stock.create_inventory_count(InventoryCountCreate(store=DEFAULT_STORES[0]),session.get(User,1),session)["count"]
        self.assertEqual(self.client.post("/api/stock/corrections",json=self.correction()).status_code,200)
        with Session(self.engine) as session:
            data=InventoryCountComplete(expected_version=0,items=[{"item_id":item["id"],"actual_qty":8} for item in sheet["items"]])
            with self.assertRaises(HTTPException) as caught:
                stock.complete_inventory_count(sheet["id"],data,session.get(User,1),session)
            self.assertEqual(caught.exception.status_code,409)
            self.assertEqual(session.get(InventoryCount,sheet["id"]).status,"draft")
            self.assertEqual(stock._current_qty(session,1,DEFAULT_STORES[0]),2)

    def test_inventory_snapshot_is_exposed_by_each_single_store_read(self):
        for url in ("/api/skus/1", "/api/skus/by-barcode/OLD-1"):
            data=self.client.get(url).json()
            self.assertTrue(data["inventory_updated_at"])
            self.assertEqual(data["quantity"],8)
        rows=self.client.get("/api/skus?store="+DEFAULT_STORES[0]).json()
        self.assertTrue(all(row["inventory_updated_at"] for row in rows))
        self.assertTrue(all(row["inventory_updated_at"] is None for row in self.client.get("/api/skus?store=all").json()))

    def test_shared_receipt_rejects_reuse_for_a_different_action(self):
        self.client.post("/api/skus/pricing/batch",json=self.pricing())
        response=self.client.post("/api/products/intake",json=self.intake(client_request_id=self.pricing()["client_request_id"]))
        self.assertEqual(response.status_code,409)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(Product)).all()),2)

    def test_historical_order_prices_are_not_changed_by_batch_pricing(self):
        with Session(self.engine) as session:
            order=SalesOrder(order_no="HISTORY",store=DEFAULT_STORES[0],total_cents=10100,item_count=1)
            session.add(order);session.flush()
            session.add(SalesOrderItem(order_id=order.id,sku_id=1,barcode="OLD-1",product_name="Original shirt",unit_price_cents=10100,subtotal_cents=10100))
            session.commit()
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=self.pricing(price=120)).status_code,200)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(SalesOrder)).one().total_cents,10100)
            self.assertEqual(session.exec(select(SalesOrderItem)).one().unit_price_cents,10100)

    def test_selection_order_does_not_break_an_identical_pricing_retry(self):
        first=self.client.post("/api/skus/pricing/batch",json=self.pricing()).json()
        data=self.pricing(); data["items"].reverse()
        self.assertEqual(self.client.post("/api/skus/pricing/batch",json=data).json(),first)


if __name__ == "__main__":
    unittest.main()

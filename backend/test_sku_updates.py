"""SKU update and audit regression checks using an isolated in-memory database."""
import json
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlmodel import Session, SQLModel, create_engine, select

# Keep route imports isolated from the application's configured database.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ADMIN"] = "false"

import database  # noqa: E402
from database import DEFAULT_STORES  # noqa: E402
from models import AuditEvent, Inventory, Product, SalesOrder, SalesOrderItem, Sku, StockLog, User  # noqa: E402
from routers.skus import update_sku  # noqa: E402
from schemas import SkuUpdate  # noqa: E402


class SkuUpdateTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            manager = User(username="sku_manager", password_hash="unused", role="manager")
            product = Product(code="SKU-UPDATE", name="Update test shirt")
            session.add_all([manager, product])
            session.flush()
            sku = Sku(
                product_id=product.id, barcode="SKU-UPDATE-01",
                color="Blue", size="M", price=100, cost=40,
            )
            session.add(sku)
            session.flush()
            session.add_all([
                Inventory(sku_id=sku.id, store=store, quantity=number * 2)
                for number, store in enumerate(DEFAULT_STORES, start=1)
            ])
            session.commit()
            self.sku_id = sku.id
            self.product_id = product.id
            self.manager_id = manager.id

    def tearDown(self):
        self.engine.dispose()

    def test_price_and_activation_changes_preserve_audit_and_inventory(self):
        expected_before = {
            "color": "Blue", "size": "M", "price": 100,
            "cost": 40, "safety_stock": 0, "active": True,
        }
        changes = [
            {"price": 129.99, "cost": 49.99},
            {"active": False},
            {"active": True},
        ]
        for index, change in enumerate(changes, start=1):
            with self.subTest(change=change):
                with Session(self.engine) as session:
                    manager = session.get(User, self.manager_id)
                    updated = update_sku(self.sku_id, SkuUpdate(**change), manager, session)
                    for key, value in change.items():
                        self.assertEqual(getattr(updated, key), value)

                expected_after = {**expected_before, **change}
                with Session(self.engine) as session:
                    saved = session.get(Sku, self.sku_id)
                    for key, value in expected_after.items():
                        self.assertEqual(getattr(saved, key), value)
                    events = session.exec(select(AuditEvent).order_by(AuditEvent.id)).all()
                    self.assertEqual(len(events), index)
                    event = events[-1]
                    self.assertEqual(event.action, "sku.update")
                    self.assertEqual(event.entity_type, "sku")
                    self.assertEqual(event.entity_id, self.sku_id)
                    self.assertEqual(event.actor_user_id, self.manager_id)
                    self.assertEqual(json.loads(event.before_json), expected_before)
                    self.assertEqual(json.loads(event.after_json), expected_after)
                    quantities = session.exec(
                        select(Inventory.quantity).order_by(Inventory.store)
                    ).all()
                    self.assertEqual(quantities, [2, 4, 6, 8])
                    self.assertEqual(session.exec(select(StockLog)).all(), [])
                    self.assertEqual(saved.barcode, "SKU-UPDATE-01")
                    self.assertEqual(saved.version, index)
                expected_before = expected_after

    def test_stale_editor_is_rejected_even_after_prices_return_to_original_values(self):
        with Session(self.engine) as session:
            manager = session.get(User, self.manager_id)
            update_sku(self.sku_id, SkuUpdate(price=120, expected_version=0), manager, session)
            update_sku(self.sku_id, SkuUpdate(price=100, expected_version=1), manager, session)
            with self.assertRaises(HTTPException) as caught:
                update_sku(self.sku_id, SkuUpdate(cost=80, expected_version=0), manager, session)
            self.assertEqual(caught.exception.status_code, 409)
            saved = session.get(Sku, self.sku_id)
            self.assertEqual((saved.price, saved.cost, saved.version), (100, 40, 2))
            self.assertEqual(len(session.exec(select(AuditEvent)).all()), 2)

    def test_writer_refreshes_cached_sku_and_uses_locked_version_conditional_update(self):
        with Session(self.engine) as session:
            manager = session.get(User, self.manager_id)
            cached = session.get(Sku, self.sku_id)
            with Session(self.engine) as other:
                update_sku(self.sku_id, SkuUpdate(cost=60), other.get(User, self.manager_id), other)
            self.assertEqual(cached.cost, 40)
            reads, writes = [], []
            run_read, run_write = session.exec, session.execute
            def read(stmt, *args, **kwargs):
                reads.append(stmt)
                return run_read(stmt, *args, **kwargs)
            def write(stmt, *args, **kwargs):
                if getattr(stmt, "is_update", False):
                    writes.append(stmt)
                return run_write(stmt, *args, **kwargs)
            with patch.object(session, "exec", side_effect=read), patch.object(session, "execute", side_effect=write):
                with self.assertRaises(HTTPException):
                    update_sku(self.sku_id, SkuUpdate(price=130, expected_version=0), manager, session)
                update_sku(self.sku_id, SkuUpdate(price=130, expected_version=1), manager, session)
            self.assertIn("FOR UPDATE", str(reads[0].compile(dialect=postgresql.dialect())))
            self.assertTrue(reads[0].get_execution_options()["populate_existing"])
            sql = str(writes[0].compile(dialect=postgresql.dialect()))
            self.assertIn("sku.version =", sql.split("WHERE")[1])
            self.assertEqual(session.get(Sku, self.sku_id).cost, 60)

    def test_price_correction_does_not_rewrite_history_or_inventory(self):
        with Session(self.engine) as session:
            order = SalesOrder(order_no="KEEP-ORDER", store=DEFAULT_STORES[0], total_cents=10000, item_count=1)
            session.add(order)
            session.flush()
            item = SalesOrderItem(order_id=order.id, sku_id=self.sku_id, barcode="SKU-UPDATE-01",
                                  product_name="Update test shirt", unit_price_cents=10000, subtotal_cents=10000)
            session.add(item)
            session.commit()
            update_sku(self.sku_id, SkuUpdate(price=129.99, cost=49.99, expected_version=0),
                       session.get(User, self.manager_id), session)
            session.refresh(order)
            session.refresh(item)
            self.assertEqual((order.total_cents, item.unit_price_cents, item.subtotal_cents), (10000, 10000, 10000))
            self.assertEqual(session.exec(select(Inventory.quantity).order_by(Inventory.store)).all(), [2, 4, 6, 8])

    def test_invalid_amounts_fail_before_write(self):
        for field in ("price", "cost"):
            for value in (-1, None, float("nan"), float("inf"), 1.001, 10**16):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    SkuUpdate(**{field: value})
            for value in (0, 9.99, "12.34"):
                self.assertEqual(getattr(SkuUpdate(**{field: value}), field), float(value))

    def test_same_values_do_not_create_another_change_or_audit(self):
        with Session(self.engine) as session:
            updated = update_sku(self.sku_id, SkuUpdate(price=100, cost=40, expected_version=0),
                                 session.get(User, self.manager_id), session)
            self.assertEqual(updated.version, 0)
            self.assertEqual(session.exec(select(AuditEvent)).all(), [])

    def test_failed_commit_rolls_back_price_version_and_audit(self):
        with Session(self.engine) as session:
            manager = session.get(User, self.manager_id)
            with patch.object(session, "commit", side_effect=RuntimeError("commit failed")), self.assertRaises(RuntimeError):
                update_sku(self.sku_id, SkuUpdate(cost=90, expected_version=0), manager, session)
        with Session(self.engine) as session:
            saved = session.get(Sku, self.sku_id)
            self.assertEqual((saved.cost, saved.version), (40, 0))
            self.assertEqual(session.exec(select(AuditEvent)).all(), [])

    def test_conditional_write_conflict_does_not_add_audit_or_change_prices(self):
        with Session(self.engine) as session:
            manager = session.get(User, self.manager_id)
            execute = session.execute
            def collide(stmt, *args, **kwargs):
                if getattr(stmt, "is_update", False):
                    return SimpleNamespace(rowcount=0)
                return execute(stmt, *args, **kwargs)
            with patch.object(session, "execute", side_effect=collide), self.assertRaises(HTTPException) as caught:
                update_sku(self.sku_id, SkuUpdate(price=120, expected_version=0), manager, session)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(session.get(Sku, self.sku_id).price, 100)
            self.assertEqual(session.exec(select(AuditEvent)).all(), [])

    def test_old_schema_upgrade_preserves_sku_and_can_run_twice(self):
        with self.engine.begin() as conn:
            conn.execute(text("alter table sku drop column version"))
        with patch.object(database, "engine", self.engine):
            database._ensure_runtime_schema()
            database._ensure_runtime_schema()
        with Session(self.engine) as session:
            saved = session.get(Sku, self.sku_id)
            self.assertEqual((saved.barcode, saved.price, saved.cost, saved.version), ("SKU-UPDATE-01", 100, 40, 0))
            self.assertEqual(session.exec(select(Inventory.quantity).order_by(Inventory.store)).all(), [2, 4, 6, 8])

    def test_variant_conflict_rolls_back_sku_and_audit(self):
        with Session(self.engine) as session:
            session.add(Sku(
                product_id=self.product_id, barcode="SKU-UPDATE-02",
                color="Red", size="L", price=100,
            ))
            session.commit()
            manager = session.get(User, self.manager_id)
            with self.assertRaises(HTTPException) as caught:
                update_sku(
                    self.sku_id,
                    SkuUpdate(color="Red", size="L", price=159.99, active=False),
                    manager, session,
                )
            self.assertEqual(caught.exception.status_code, 400)

        with Session(self.engine) as session:
            saved = session.get(Sku, self.sku_id)
            self.assertEqual((saved.color, saved.size, saved.price, saved.active),
                             ("Blue", "M", 100, True))
            self.assertEqual(session.exec(select(AuditEvent)).all(), [])


if __name__ == "__main__":
    unittest.main()

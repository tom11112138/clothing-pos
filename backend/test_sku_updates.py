"""SKU update and audit regression checks using an isolated in-memory database."""
import json
import os
import unittest

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

# Keep route imports isolated from the application's configured database.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ADMIN"] = "false"

from database import DEFAULT_STORES  # noqa: E402
from models import AuditEvent, Inventory, Product, Sku, StockLog, User  # noqa: E402
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
                expected_before = expected_after

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

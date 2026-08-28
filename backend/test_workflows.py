"""Regression checks for the inventory and authorization rules.

Run from backend with: python -m unittest test_workflows.py
"""
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

DB_PATH = Path(tempfile.gettempdir()) / "clothing_pos_workflow_test.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["SEED_ADMIN"] = "false"

from sqlmodel import Session, select  # noqa: E402

from database import engine, init_db  # noqa: E402
from models import Inventory, Product, SalesOrder, Sku, User  # noqa: E402
from routers.orders import refund_order  # noqa: E402
from routers.stock import (complete_inventory_count, create_inventory_count,
                           create_sale, create_transfer_batch,
                           receive_transfer_batch, stock_in,
                           update_inventory_count_item)  # noqa: E402
from schemas import (InventoryCountCreate, InventoryCountFinish,
                      InventoryCountLineUpdate, RefundCreate, SaleCreate, SaleItem,
                      StockMove, TransferBatchCreate, TransferBatchItemCreate,
                      TransferBatchReceive, TransferBatchReceiveItem)  # noqa: E402
from security import hash_password  # noqa: E402


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        with Session(engine) as session:
            manager = User(username="manager", password_hash=hash_password("password"), role="manager")
            staff = User(username="staff2", password_hash=hash_password("password"), role="staff", store="2号店")
            product = Product(code="TEST001", name="Test shirt")
            session.add_all([manager, staff, product])
            session.commit()
            session.refresh(manager)
            session.refresh(staff)
            session.refresh(product)
            sku = Sku(product_id=product.id, barcode="TEST00101", price=100, quantity=0)
            session.add(sku)
            session.commit()
            session.refresh(sku)
            session.add_all([
                Inventory(sku_id=sku.id, store="1号店", quantity=10),
                Inventory(sku_id=sku.id, store="2号店", quantity=0),
                Inventory(sku_id=sku.id, store="3号店", quantity=0),
                Inventory(sku_id=sku.id, store="4号店", quantity=0),
            ])
            session.commit()
            cls.sku_id = sku.id
            cls.manager_id = manager.id
            cls.staff_id = staff.id

    @classmethod
    def tearDownClass(cls):
        engine.dispose()
        if DB_PATH.exists():
            DB_PATH.unlink()

    def test_refund_and_transfer_workflow(self):
        with Session(engine) as session:
            manager = session.get(User, self.manager_id)
            sale = create_sale(SaleCreate(
                store="1号店", client_request_id="test-sale-request-0001",
                items=[SaleItem(sku_id=self.sku_id, qty=1)]
            ), manager, session)
            self.assertTrue(sale["ok"])
            order_id = session.exec(select(SalesOrder.id)).first()

            refund = refund_order(order_id, RefundCreate(
                store="1号店", client_request_id="test-refund-request-0001",
                items=[SaleItem(sku_id=self.sku_id, qty=1)]
            ), manager, session)
            self.assertEqual(refund["refund_total"], 100)
            refund_retry = refund_order(order_id, RefundCreate(
                store="1号店", client_request_id="test-refund-request-0001",
                items=[SaleItem(sku_id=self.sku_id, qty=1)]
            ), manager, session)
            self.assertTrue(refund_retry["duplicate"])
            self.assertEqual(refund_retry["refund_no"], refund["refund_no"])

            transfer = create_transfer_batch(TransferBatchCreate(
                from_store="1号店", to_store="2号店",
                client_request_id="test-transfer-request-0001",
                items=[TransferBatchItemCreate(sku_id=self.sku_id, qty=2)],
            ), manager, session)["transfer"]
            transfer_retry = create_transfer_batch(TransferBatchCreate(
                from_store="1号店", to_store="2号店",
                client_request_id="test-transfer-request-0001",
                items=[TransferBatchItemCreate(sku_id=self.sku_id, qty=2)],
            ), manager, session)
            self.assertTrue(transfer_retry["duplicate"])
            self.assertEqual(transfer_retry["transfer"]["id"], transfer["id"])
            item_id = transfer["items"][0]["id"]
            self.assertEqual(transfer["status"], "shipped")

            partial = receive_transfer_batch(transfer["id"], TransferBatchReceive(items=[
                TransferBatchReceiveItem(item_id=item_id, received_qty=1)
            ]), manager, session)["transfer"]
            self.assertEqual(partial["status"], "partial")
            finished = receive_transfer_batch(transfer["id"], TransferBatchReceive(items=[
                TransferBatchReceiveItem(item_id=item_id, received_qty=1)
            ]), manager, session)["transfer"]
            self.assertEqual(finished["status"], "received")

            count = create_inventory_count(InventoryCountCreate(store="2号店"), manager, session)["count"]
            count_item = count["items"][0]
            update_inventory_count_item(count["id"], count_item["id"], InventoryCountLineUpdate(actual_qty=2), manager, session)
            completed = complete_inventory_count(count["id"], InventoryCountFinish(), manager, session)["count"]
            self.assertEqual(completed["status"], "completed")

    def test_stock_in_is_idempotent(self):
        with Session(engine) as session:
            manager = session.get(User, self.manager_id)
            before = session.exec(select(Inventory).where(
                Inventory.sku_id == self.sku_id, Inventory.store == "3号店"
            )).first().quantity
            move = StockMove(
                sku_id=self.sku_id, qty=3, store="3号店",
                client_request_id="test-stock-in-request-0001",
            )
            first = stock_in(move, manager, session)
            second = stock_in(move, manager, session)
            self.assertEqual(first["quantity"], before + 3)
            self.assertTrue(second["duplicate"])
            self.assertEqual(second["quantity"], before + 3)

    def test_staff_cannot_sell_another_store(self):
        with Session(engine) as session:
            staff = session.get(User, self.staff_id)
            with self.assertRaises(HTTPException) as caught:
                create_sale(SaleCreate(
                    store="1号店", client_request_id="test-forbidden-sale-0001",
                    items=[SaleItem(sku_id=self.sku_id, qty=1)],
                ), staff, session)
            self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()

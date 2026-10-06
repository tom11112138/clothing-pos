"""Sequential receipt retry checks; no production database or load tests."""
import json
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ADMIN"] = "false"

from database import DEFAULT_STORES  # noqa: E402
from models import (AuditEvent, Inventory, Product, Sku, StockLog,
                    StockTransferBatch, StockTransferItem, StockTransferReceipt, User)  # noqa: E402
from routers import stock  # noqa: E402
from schemas import (TransferBatchCreate, TransferBatchItemCreate,
                     TransferBatchReceive, TransferBatchReceiveItem)  # noqa: E402


class TransferReceiptTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            manager = User(username="receipt_manager", password_hash="unused", role="manager")
            other = User(username="other_manager", password_hash="unused", role="manager")
            product = Product(code="RECEIPT", name="Receipt test shirt")
            session.add_all([manager, other, product])
            session.flush()
            self.sku_ids = []
            for number in range(2):
                sku = Sku(product_id=product.id, barcode=f"RECEIPT-{number}")
                session.add(sku)
                session.flush()
                self.sku_ids.append(sku.id)
                session.add_all([
                    Inventory(sku_id=sku.id, store=store, quantity=20 if i == 0 else 0)
                    for i, store in enumerate(DEFAULT_STORES)
                ])
            session.commit()
            self.manager_id, self.other_id = manager.id, other.id
            batch = stock.create_transfer_batch(TransferBatchCreate(
                from_store=DEFAULT_STORES[0], to_store=DEFAULT_STORES[1],
                client_request_id="receipt-test-shipment-001",
                items=[TransferBatchItemCreate(sku_id=sid, qty=10) for sid in self.sku_ids],
            ), manager, session)["transfer"]
            self.batch_id = batch["id"]
            self.item_ids = [row["id"] for row in batch["items"]]

    def tearDown(self):
        self.engine.dispose()

    def request(self, request_id="receipt-test-request-001", received=3, rejected=0, **kwargs):
        return TransferBatchReceive(
            client_request_id=request_id,
            items=[TransferBatchReceiveItem(
                item_id=self.item_ids[0], received_qty=received, rejected_qty=rejected,
            )], **kwargs,
        )

    def receive(self, request, *, user_id=None, batch_id=None):
        with Session(self.engine) as session:
            return stock.receive_transfer_batch(
                self.batch_id if batch_id is None else batch_id,
                request, session.get(User, user_id or self.manager_id), session,
            )

    def snapshot(self):
        with Session(self.engine) as session:
            return {
                "inventory": [tuple(row) for row in session.exec(
                    select(Inventory.sku_id, Inventory.store, Inventory.quantity)
                    .order_by(Inventory.sku_id, Inventory.store)
                ).all()],
                "items": [tuple(row) for row in session.exec(
                    select(StockTransferItem.id, StockTransferItem.received_qty, StockTransferItem.rejected_qty)
                    .order_by(StockTransferItem.id)
                ).all()],
                "status": session.get(StockTransferBatch, self.batch_id).status,
                "log_count": len(session.exec(select(StockLog)).all()),
                "audit_count": len(session.exec(select(AuditEvent)).all()),
                "receipt_count": len(session.exec(select(StockTransferReceipt)).all()),
            }

    def test_repeated_partial_receipt_does_not_duplicate_stock_or_logs(self):
        request = self.request(received=3, rejected=2)
        first = self.receive(request)
        before = self.snapshot()
        second = self.receive(request)
        self.assertEqual(second, {**first, "duplicate": True})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(first["transfer"]["items"][0]["received_qty"], 3)
        self.assertEqual(first["transfer"]["items"][0]["rejected_qty"], 2)
        self.assertIn((self.sku_ids[0], DEFAULT_STORES[0], 12), before["inventory"])
        self.assertIn((self.sku_ids[0], DEFAULT_STORES[1], 3), before["inventory"])
        with Session(self.engine) as session:
            record = session.exec(select(StockTransferReceipt)).one()
            self.assertEqual(record.operator_user_id, self.manager_id)
            self.assertEqual(json.loads(record.request_json)["items"],
                             [item.model_dump() for item in request.items])
            audit = session.exec(select(AuditEvent).where(
                AuditEvent.action == "transfer_batch.receive"
            )).one()
            self.assertEqual(audit.request_id, request.client_request_id)
            self.assertEqual(json.loads(audit.detail_json)["receipt_id"], record.id)

    def test_new_request_can_receive_same_quantity_again(self):
        self.receive(self.request())
        second = self.receive(self.request(request_id="receipt-test-request-002"))
        self.assertFalse(second["duplicate"])
        self.assertEqual(second["transfer"]["items"][0]["received_qty"], 6)
        self.assertEqual(self.snapshot()["receipt_count"], 2)

    def test_retries_return_original_snapshot_after_batch_is_completed(self):
        partial_request = self.request()
        partial = self.receive(partial_request)
        final_request = TransferBatchReceive(
            client_request_id="receipt-test-request-final",
            items=[
                TransferBatchReceiveItem(item_id=self.item_ids[0], received_qty=7),
                TransferBatchReceiveItem(item_id=self.item_ids[1], received_qty=10),
            ],
        )
        finished = self.receive(final_request)
        self.assertEqual(finished["transfer"]["status"], "received")
        before = self.snapshot()
        self.assertEqual(self.receive(final_request), {**finished, "duplicate": True})
        self.assertEqual(self.receive(partial_request), {**partial, "duplicate": True})
        self.assertEqual(self.snapshot(), before)

    def test_reordered_lines_are_the_same_request(self):
        request = TransferBatchReceive(
            client_request_id="receipt-test-request-sorted",
            items=[TransferBatchReceiveItem(item_id=iid, received_qty=2) for iid in self.item_ids],
        )
        first = self.receive(request)
        reversed_request = request.model_copy(update={"items": list(reversed(request.items))})
        self.assertEqual(self.receive(reversed_request), {**first, "duplicate": True})

    def test_reused_key_rejects_changed_content_or_operator(self):
        self.receive(self.request())
        before = self.snapshot()
        cases = [
            (self.request(received=4), self.manager_id),
            (self.request(received=2, rejected=1), self.manager_id),
            (self.request(note="Different note"), self.manager_id),
            (self.request(), self.other_id),
        ]
        for request, user_id in cases:
            with self.subTest(request=request, user_id=user_id):
                with self.assertRaises(HTTPException) as caught:
                    self.receive(request, user_id=user_id)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(self.snapshot(), before)

    def test_request_key_cannot_be_reused_on_another_batch(self):
        self.receive(self.request())
        with Session(self.engine) as session:
            other = stock.create_transfer_batch(TransferBatchCreate(
                from_store=DEFAULT_STORES[0], to_store=DEFAULT_STORES[2],
                client_request_id="receipt-test-shipment-002",
                items=[TransferBatchItemCreate(sku_id=self.sku_ids[0], qty=2)],
            ), session.get(User, self.manager_id), session)["transfer"]
        before = self.snapshot()
        with self.assertRaises(HTTPException) as caught:
            self.receive(self.request(), batch_id=other["id"])
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_lines_do_not_change_any_stock(self):
        before = self.snapshot()
        invalid_lines = [
            TransferBatchReceiveItem(item_id=self.item_ids[1], received_qty=11),
            TransferBatchReceiveItem(item_id=999999, received_qty=1),
            TransferBatchReceiveItem(item_id=self.item_ids[0], received_qty=1),
            TransferBatchReceiveItem(item_id=self.item_ids[1]),
        ]
        for invalid in invalid_lines:
            with self.subTest(invalid=invalid):
                request = self.request()
                request.items.append(invalid)
                with self.assertRaises(HTTPException) as caught:
                    self.receive(request)
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.receive(self.request())["ok"])

    def test_failed_transaction_does_not_reserve_key_or_change_inventory(self):
        before = self.snapshot()
        real_adjust = stock._adjust_stock
        calls = 0

        def fail_after_first_change(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Simulated write failure")
            return real_adjust(*args, **kwargs)

        request = self.request(received=3, rejected=2)
        with patch.object(stock, "_adjust_stock", side_effect=fail_after_first_change):
            with self.assertRaises(RuntimeError):
                self.receive(request)
        self.assertEqual(self.snapshot(), before)
        result = self.receive(request)
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["transfer"]["items"][0]["received_qty"], 3)

    def test_unique_key_conflict_returns_committed_result(self):
        request = self.request()
        first = self.receive(request)
        before = self.snapshot()
        find_retry = stock._receipt_retry_result
        calls = 0

        def miss_first_lookup(*args, **kwargs):
            nonlocal calls
            calls += 1
            return None if calls == 1 else find_retry(*args, **kwargs)

        with patch.object(stock, "_receipt_retry_result", side_effect=miss_first_lookup):
            self.assertEqual(self.receive(request), {**first, "duplicate": True})
        self.assertEqual(self.snapshot(), before)

    def test_request_id_is_required(self):
        with self.assertRaises(ValidationError):
            TransferBatchReceive(items=[TransferBatchReceiveItem(
                item_id=self.item_ids[0], received_qty=1,
            )])


if __name__ == "__main__":
    unittest.main()

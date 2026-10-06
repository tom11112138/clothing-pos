"""Deterministic count-sheet regression tests; no production database or load tests."""
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlmodel import Session, SQLModel, create_engine, select

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ADMIN"] = "false"

import database  # noqa: E402
from models import (AuditEvent, Inventory, InventoryCount, InventoryCountItem,
                    Product, Sku, StockLog, User)  # noqa: E402
from routers import stock  # noqa: E402
from schemas import (InventoryCountComplete, InventoryCountCompleteItem,
                     InventoryCountCreate, InventoryCountFinish, InventoryCountLineUpdate)  # noqa: E402


class InventoryCountTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.store = database.DEFAULT_STORES[0]
        with Session(self.engine) as session:
            manager = User(username="count_manager", password_hash="unused", role="manager")
            other = User(username="second_manager", password_hash="unused", role="manager")
            product = Product(code="COUNT", name="Count test shirt")
            session.add_all([manager, other, product])
            session.flush()
            self.sku_ids = []
            for number in range(2):
                sku = Sku(product_id=product.id, barcode=f"COUNT-{number}")
                session.add(sku)
                session.flush()
                self.sku_ids.append(sku.id)
                session.add(Inventory(sku_id=sku.id, store=self.store, quantity=10))
            session.commit()
            self.manager_id, self.other_id = manager.id, other.id
            self.count = stock.create_inventory_count(
                InventoryCountCreate(store=self.store), manager, session,
            )["count"]
        self.count_id = self.count["id"]
        self.item_ids = [item["id"] for item in self.count["items"]]

    def tearDown(self):
        self.engine.dispose()

    def request(self, *, version=0, quantities=(8, 12), item_ids=None):
        return InventoryCountComplete(expected_version=version, items=[
            InventoryCountCompleteItem(item_id=item_id, actual_qty=qty)
            for item_id, qty in zip(item_ids or self.item_ids, quantities)
        ])

    def complete(self, data=None):
        with Session(self.engine) as session:
            return stock.complete_inventory_count(
                self.count_id, data or self.request(), session.get(User, self.manager_id), session,
            )

    def edit(self, *, version=0, actual=7, item_id=None):
        with Session(self.engine) as session:
            return stock.update_inventory_count_item(
                self.count_id, item_id or self.item_ids[0],
                InventoryCountLineUpdate(expected_version=version, actual_qty=actual),
                session.get(User, self.other_id), session,
            )

    def cancel(self, *, version=0):
        with Session(self.engine) as session:
            return stock.cancel_inventory_count(
                self.count_id, InventoryCountFinish(expected_version=version),
                session.get(User, self.manager_id), session,
            )

    def snapshot(self):
        with Session(self.engine) as session:
            count = session.get(InventoryCount, self.count_id)
            return {
                "state": (count.status, count.version, count.note, count.completed_by, count.cancelled_by),
                "items": [tuple(row) for row in session.exec(select(
                    InventoryCountItem.id, InventoryCountItem.actual_qty,
                ).where(InventoryCountItem.count_id == self.count_id).order_by(InventoryCountItem.id))],
                "stock": [tuple(row) for row in session.exec(select(
                    Inventory.sku_id, Inventory.quantity, Inventory.updated_at,
                ).where(Inventory.store == self.store).order_by(Inventory.sku_id))],
                "logs": len(session.exec(select(StockLog)).all()),
                "audits": len(session.exec(select(AuditEvent)).all()),
            }

    def test_atomic_completion_stores_actuals_stock_logs_and_version(self):
        result = self.complete()["count"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["version"], 1)
        self.assertEqual([row["actual_qty"] for row in result["items"]], [8, 12])
        snapshot = self.snapshot()
        self.assertEqual([row[1] for row in snapshot["stock"]], [8, 12])
        self.assertEqual(snapshot["logs"], 2)
        with Session(self.engine) as session:
            logs = session.exec(select(StockLog).order_by(StockLog.sku_id)).all()
            self.assertEqual([log.change for log in logs], [-2, 2])
            self.assertTrue(all(log.count_id == self.count_id for log in logs))
            self.assertEqual(len(session.exec(select(AuditEvent).where(
                AuditEvent.action == "inventory_count.complete"
            )).all()), 1)

    def test_zero_actual_is_valid_and_unchanged_lines_do_not_create_stock_logs(self):
        self.complete(self.request(quantities=(0, 10)))
        self.assertEqual([row[1] for row in self.snapshot()["stock"]], [0, 10])
        self.assertEqual(self.snapshot()["logs"], 1)

    def test_edit_bumps_version_without_changing_inventory(self):
        before = self.snapshot()
        saved = self.edit()
        self.assertEqual(saved["version"], 1)
        after = self.snapshot()
        self.assertEqual(after["stock"], before["stock"])
        self.assertEqual(after["logs"], 0)
        self.assertEqual(after["audits"], before["audits"] + 1)
        self.assertEqual(after["items"][0][1], 7)
        self.assertEqual(self.complete(self.request(version=1))["count"]["version"], 2)

    def test_stale_editor_completion_and_cancellation_are_rejected(self):
        self.edit()
        before = self.snapshot()
        for operation in (self.edit, self.complete, self.cancel):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(HTTPException) as caught:
                    operation()
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(self.snapshot(), before)

    def test_finished_sheet_is_immutable_and_retry_does_not_repeat_adjustment(self):
        for finish in ("complete", "cancel"):
            with self.subTest(finish=finish):
                if finish == "cancel":
                    # Reset only this test fixture to exercise the other terminal state.
                    self.tearDown()
                    self.setUp()
                getattr(self, finish)()
                before = self.snapshot()
                for operation in (lambda: self.edit(version=1),
                                  lambda: self.complete(self.request(version=1)),
                                  lambda: self.cancel(version=1)):
                    with self.assertRaises(HTTPException) as caught:
                        operation()
                    self.assertEqual(caught.exception.status_code, 409)
                    self.assertEqual(self.snapshot(), before)

    def test_invalid_item_sets_leave_all_rows_unchanged(self):
        before = self.snapshot()
        for item_ids in ([self.item_ids[0]], [self.item_ids[0], self.item_ids[0]],
                         [self.item_ids[0], 999999]):
            with self.subTest(item_ids=item_ids):
                with self.assertRaises(HTTPException) as caught:
                    self.complete(self.request(item_ids=item_ids))
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(self.snapshot(), before)

    def test_item_from_another_sheet_cannot_be_edited(self):
        with Session(self.engine) as session:
            other = stock.create_inventory_count(
                InventoryCountCreate(store=database.DEFAULT_STORES[1]),
                session.get(User, self.manager_id), session,
            )["count"]
        before = self.snapshot()
        with self.assertRaises(HTTPException) as caught:
            self.edit(item_id=other["items"][0]["id"])
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.snapshot(), before)

    def test_stock_change_rejects_entire_submission_including_actuals(self):
        with Session(self.engine) as session:
            inv = session.exec(select(Inventory).where(Inventory.sku_id == self.sku_ids[1])).one()
            inv.quantity -= 1
            inv.updated_at += timedelta(seconds=1)
            session.add(inv)
            session.commit()
        before = self.snapshot()
        with self.assertRaises(HTTPException) as caught:
            self.complete()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_late_inventory_conflict_rolls_back_already_written_lines(self):
        before = self.snapshot()
        with Session(self.engine) as session:
            execute = session.execute
            written = []

            def conflict_on_second(statement, *args, **kwargs):
                if isinstance(statement, Update) and statement.table.name == "inventory":
                    written.append(statement)
                    if len(written) == 2:
                        return SimpleNamespace(rowcount=0)
                return execute(statement, *args, **kwargs)

            with patch.object(session, "execute", side_effect=conflict_on_second):
                with self.assertRaises(HTTPException) as caught:
                    stock.complete_inventory_count(self.count_id, self.request(),
                                                   session.get(User, self.manager_id), session)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(len(written), 2)
            self.assertFalse(session.in_transaction())
        self.assertEqual(self.snapshot(), before)

    def test_commit_failure_rolls_back_completion(self):
        before = self.snapshot()
        with Session(self.engine) as session:
            manager = session.get(User, self.manager_id)
            with patch.object(session, "commit", side_effect=RuntimeError("Write failed")):
                with self.assertRaises(RuntimeError):
                    stock.complete_inventory_count(self.count_id, self.request(), manager, session)
            self.assertFalse(session.in_transaction())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.complete()["count"]["status"], "completed")

    def test_cached_draft_is_refreshed_before_editing(self):
        with Session(self.engine, expire_on_commit=False) as session:
            cached = session.get(InventoryCount, self.count_id)
            session.execute(update(InventoryCount).where(InventoryCount.id == self.count_id)
                            .values(status="cancelled", version=1)
                            .execution_options(synchronize_session=False))
            session.commit()
            self.assertEqual(cached.status, "draft")
            with self.assertRaises(HTTPException) as caught:
                stock.update_inventory_count_item(
                    self.count_id, self.item_ids[0], InventoryCountLineUpdate(expected_version=0, actual_qty=5),
                    session.get(User, self.manager_id), session,
                )
            self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNone(self.snapshot()["items"][0][1])

    def test_all_writers_lock_and_refresh_header_before_other_rows(self):
        for action in ("edit", "complete", "cancel"):
            with self.subTest(action=action), Session(self.engine) as session:
                manager = session.get(User, self.manager_id)
                statements = []
                execute = session.exec

                def record(statement, *args, **kwargs):
                    statements.append(statement)
                    return execute(statement, *args, **kwargs)

                # A stale version stops before mutation while exercising the real locking query.
                with patch.object(session, "exec", side_effect=record), self.assertRaises(HTTPException):
                    if action == "edit":
                        stock.update_inventory_count_item(self.count_id, self.item_ids[0],
                            InventoryCountLineUpdate(expected_version=99, actual_qty=5), manager, session)
                    elif action == "complete":
                        stock.complete_inventory_count(self.count_id, self.request(version=99), manager, session)
                    else:
                        stock.cancel_inventory_count(self.count_id,
                            InventoryCountFinish(expected_version=99), manager, session)
                sql = str(statements[0].compile(dialect=postgresql.dialect()))
                self.assertIn("FROM inventorycount", sql)
                self.assertIn("FOR UPDATE", sql)
                self.assertTrue(statements[0].get_execution_options()["populate_existing"])

    def test_legacy_requests_without_version_or_complete_items_are_rejected(self):
        for schema, data in ((InventoryCountLineUpdate, {"actual_qty": 3}),
                             (InventoryCountFinish, {}),
                             (InventoryCountComplete, {"expected_version": 0}),
                             (InventoryCountComplete, {"expected_version": 0, "items": []})):
            with self.subTest(schema=schema.__name__, data=data), self.assertRaises(ValidationError):
                schema.model_validate(data)


class CountSchemaUpgradeTests(unittest.TestCase):
    def test_existing_database_gets_version_once_without_changing_counts(self):
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                connection.execute(text("create table inventorycount (id integer primary key, store varchar, status varchar)"))
                connection.execute(text("insert into inventorycount (id, store, status) values (1, 'test', 'draft')"))
            SQLModel.metadata.create_all(engine)
            with patch.object(database, "engine", engine):
                database._ensure_runtime_schema()
                database._ensure_runtime_schema()
            with engine.begin() as connection:
                row = connection.execute(text("select store, status, version from inventorycount where id=1")).one()
                self.assertEqual(tuple(row), ("test", "draft", 0))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()

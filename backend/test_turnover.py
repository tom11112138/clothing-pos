"""Isolated functional checks for payment snapshots, permissions and cash-flow periods."""
import hashlib
import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlmodel import Session, SQLModel, create_engine, select

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ADMIN"] = "false"

import database  # noqa: E402
from models import AuditEvent, Inventory, Product, SalesOrder, SalesRefund, Sku, User  # noqa: E402
from routers import orders  # noqa: E402
from routers.stock import create_sale  # noqa: E402
from schemas import SaleCreate, SaleItem  # noqa: E402


class TurnoverTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.manager = User(username="admin", role="manager", password_hash="unused")
        self.staff = User(username="staff2", role="staff", store="2号店", password_hash="unused")
        product = Product(code="FLOW", name="Flow shirt")
        self.session.add_all([self.manager, self.staff, product])
        self.session.flush()
        self.sku = Sku(product_id=product.id, barcode="FLOW01", price=12.34)
        self.session.add(self.sku)
        self.session.flush()
        self.session.add_all([Inventory(sku_id=self.sku.id, store=store, quantity=1000)
                              for store in database.DEFAULT_STORES])
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def order(self, *, store="1号店", when="2026-10-10T16:00:00", cents=1234, method="cash"):
        order = SalesOrder(order_no=f"ORDER-{len(self.session.exec(select(SalesOrder)).all())}",
                           store=store, operator="test", payment_method=method,
                           total_cents=cents, item_count=2, created_at=datetime.fromisoformat(when))
        self.session.add(order)
        self.session.commit()
        return order

    def refund(self, order, *, store="2号店", when="2026-10-11T00:00:00", cents=600):
        suffix = len(self.session.exec(select(SalesRefund)).all())
        self.session.add(SalesRefund(refund_no=f"RF-{suffix}", client_request_id=f"refund-{suffix:016}",
                                     request_hash="unused", order_id=order.id, store=store,
                                     operator="test", total_cents=cents,
                                     created_at=datetime.fromisoformat(when)))
        self.session.commit()

    def report(self, *, period="day", date="2026-10-11", store=None, user=None):
        return orders.get_turnover(period, date, store, user or self.manager, self.session)

    def test_four_methods_and_notes_are_snapshotted_and_audited(self):
        for index, method in enumerate(("cash", "wechat", "alipay", "card")):
            result = create_sale(SaleCreate(
                store="1号店", payment_method=method, note="  gift\n<script>literal</script>  ",
                items=[SaleItem(sku_id=self.sku.id)], client_request_id=f"payment-test-{index:016}",
            ), self.manager, self.session)
            order = self.session.exec(select(SalesOrder).where(SalesOrder.order_no == result["order_no"])).one()
            self.assertEqual(result["payment_method"], method)
            self.assertEqual(order.note, "gift\n<script>literal</script>")
            detail = orders.get_order(order.id, self.manager, self.session)
            self.assertEqual(detail["payment_method"], method)
            self.assertEqual(detail["note"], order.note)
        audits = self.session.exec(select(AuditEvent).where(AuditEvent.action == "sale.create")).all()
        self.assertEqual(len(audits), 4)
        self.assertEqual(json.loads(audits[-1].detail_json)["payment_method"], "card")

    def test_retry_is_exactly_once_and_rejects_changed_payment_or_note(self):
        payload = dict(store="1号店", payment_method="wechat", note="gift",
                       items=[SaleItem(sku_id=self.sku.id, qty=2)], client_request_id="sale-retry-0000000001")
        first = create_sale(SaleCreate(**payload), self.manager, self.session)
        second = create_sale(SaleCreate(**payload), self.manager, self.session)
        self.assertEqual(first, second)
        for changes in ({"payment_method": "cash"}, {"note": "different"}):
            with self.assertRaises(HTTPException) as caught:
                create_sale(SaleCreate(**{**payload, **changes}), self.manager, self.session)
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(self.session.exec(select(SalesOrder)).all()), 1)
        inv = self.session.exec(select(Inventory).where(Inventory.store == "1号店")).one()
        self.assertEqual(inv.quantity, 998)
        self.sku.active = False
        self.session.add(self.sku)
        self.session.commit()
        self.assertEqual(create_sale(SaleCreate(**payload), self.manager, self.session), first)

    def test_invalid_methods_and_oversized_notes_fail_validation(self):
        for changes in ({"payment_method": "crypto"}, {"payment_method": None}, {"note": "x" * 501}):
            with self.assertRaises(ValidationError):
                SaleCreate(client_request_id="bad-sale-00000000001", items=[SaleItem(sku_id=1)], **changes)

    def test_day_uses_chinese_midnight_and_half_open_bounds(self):
        self.order(when="2026-10-10T15:59:59.999999", cents=1)
        self.order(when="2026-10-10T16:00:00", cents=2)
        self.order(when="2026-10-11T15:59:59.999999", cents=3)
        self.order(when="2026-10-11T16:00:00", cents=4)
        result = self.report()
        self.assertEqual(result["summary"]["sales_cents"], 5)
        self.assertEqual(result["summary"]["order_count"], 2)
        self.assertEqual(result["timezone"], "Asia/Shanghai")

    def test_cross_month_refund_uses_actual_refund_day_store_and_original_method(self):
        old = self.order(when="2026-09-20T00:00:00", method="wechat", cents=1000)
        old.status, old.refunded_cents = "partial_refunded", 600
        self.session.add(old)
        self.session.commit()
        self.refund(old)
        self.order(store="2号店", cents=200, method="alipay")
        result = self.report(user=self.staff)
        self.assertEqual([row["store"] for row in result["stores"]], ["2号店"])
        self.assertEqual(result["summary"]["sales_cents"], 200)
        self.assertEqual(result["summary"]["refund_cents"], 600)
        self.assertEqual(result["summary"]["net_cents"], -400)
        wechat = next(row for row in result["payments"] if row["payment_method"] == "wechat")
        self.assertEqual(wechat["refund_cents"], 600)
        previous = self.report(period="month", date="2026-09")
        self.assertEqual(previous["summary"]["sales_cents"], 1000)
        self.assertEqual(previous["summary"]["refund_cents"], 0)

    def test_month_rollup_includes_zero_days_and_matches_daily_reports(self):
        self.order(when="2024-02-28T16:00:00", cents=4)
        self.order(when="2024-02-29T16:00:00", cents=8)
        month = self.report(period="month", date="2024-02")
        self.assertEqual(len(month["days"]), 29)
        self.assertEqual(month["summary"]["sales_cents"], 4)
        self.assertEqual(month["days"][-1]["date"], "2024-02-29")
        self.assertEqual(month["days"][0]["sales_cents"], 0)
        self.assertEqual(month["summary"]["sales_cents"], sum(d["sales_cents"] for d in month["days"]))
        december = self.report(period="month", date="2026-12")
        self.assertEqual(december["date_to"], "2026-12-31")

    def test_manager_and_store_manager_can_view_four_stores(self):
        for store in database.DEFAULT_STORES:
            self.order(store=store, cents=100)
        for binding in (None, "3号店"):
            self.manager.store = binding
            result = self.report()
            self.assertEqual(len(result["stores"]), 4)
            self.assertEqual(result["summary"]["sales_cents"], 400)
        single = self.report(store="3号店")
        self.assertEqual(single["summary"]["sales_cents"], 100)

    def test_staff_cannot_request_other_stores_or_all(self):
        self.order(store="1号店", cents=900)
        own = self.order(store="2号店", cents=100)
        for period, date in (("day", "2026-10-11"), ("month", "2026-10")):
            self.assertEqual(self.report(period=period, date=date, user=self.staff)["summary"]["sales_cents"], 100)
            for store in ("1号店", "all", "bad"):
                with self.assertRaises(HTTPException) as caught:
                    self.report(period=period, date=date, store=store, user=self.staff)
                self.assertEqual(caught.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            orders.list_orders(store="1号店", limit=200, user=self.staff, session=self.session)
        self.assertEqual(orders.get_order(own.id, self.staff, self.session)["store"], "2号店")

    def test_unbound_staff_cannot_read_even_unassigned_orders(self):
        unassigned = self.order(store=None)
        for binding in (None, "invalid"):
            self.staff.store = binding
            with self.assertRaises(HTTPException) as caught:
                self.report(user=self.staff)
            self.assertEqual(caught.exception.status_code, 403)
            with self.assertRaises(HTTPException):
                orders.get_order(unassigned.id, self.staff, self.session)

    def test_historical_unknown_payment_is_not_counted_as_cash(self):
        self.order(method=None, cents=111)
        self.order(method="legacy", cents=222)
        result = self.report()
        by_method = {row["payment_method"]: row for row in result["payments"]}
        self.assertEqual(by_method["unrecorded"]["sales_cents"], 333)
        self.assertEqual(by_method["cash"]["sales_cents"], 0)

    def test_list_limit_does_not_truncate_reports_or_order_summary(self):
        self.session.add_all([SalesOrder(order_no=f"bulk-{i}", store="1号店", payment_method="card",
                                         total_cents=1, item_count=1,
                                         created_at=datetime(2026, 10, 11)) for i in range(501)])
        self.session.commit()
        listing = orders.list_orders(limit=200, user=self.manager, session=self.session)
        self.assertEqual(len(listing["orders"]), 200)
        self.assertEqual(listing["summary"]["count"], 501)
        self.assertTrue(listing["has_more"])
        self.assertEqual(self.report()["summary"]["sales_cents"], 501)

    def test_bad_dates_and_store_are_rejected(self):
        for period, value in (("day", "2026-02-30"), ("day", "20261011"),
                              ("month", "2026-13"), ("month", "2026-2"), ("week", "2026-10"),
                              ("day", "0001-01-01"), ("month", "9999-12")):
            with self.assertRaises(HTTPException) as caught:
                self.report(period=period, date=value)
            self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            self.report(store="invalid")
        with self.assertRaises(HTTPException):
            orders.list_orders(date_from="2026-10-12", date_to="2026-10-11",
                               limit=200, user=self.manager, session=self.session)

    def test_report_is_one_consistent_aggregate_query_and_compiles_for_postgres(self):
        self.order()
        self.session.refresh(self.manager)
        calls = []
        execute = self.session.execute
        def capture(stmt, *args, **kwargs):
            compiled = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            self.assertIn("UNION ALL", compiled)
            calls.append(compiled)
            return execute(stmt, *args, **kwargs)
        with patch.object(self.session, "execute", side_effect=capture):
            result = self.report(period="month", date="2026-10")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["summary"]["sales_cents"], 1234)

    def test_schema_upgrade_is_repeatable_and_does_not_invent_payment_history(self):
        old = self.order(method=None)
        with self.engine.begin() as conn:
            conn.execute(text("alter table salesorder drop column payment_method"))
        with patch.object(database, "engine", self.engine):
            database._ensure_runtime_schema()
            database._ensure_runtime_schema()
        self.session.expire_all()
        self.assertIsNone(self.session.get(SalesOrder, old.id).payment_method)
        self.assertEqual(self.session.exec(select(Inventory).where(Inventory.store == "1号店")).one().quantity, 1000)

    def test_legacy_request_hash_can_be_retried_without_relabelling_cash(self):
        first = create_sale(SaleCreate(client_request_id="legacy-sale-00000001", store="1号店",
                                      items=[SaleItem(sku_id=self.sku.id)]), self.manager, self.session)
        order = self.session.exec(select(SalesOrder)).one()
        content = {"store": "1号店", "user_id": self.manager.id, "items": [[self.sku.id, 1]]}
        order.payment_method = None
        order.request_hash = hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.session.add(order)
        self.session.commit()
        replay = create_sale(SaleCreate(client_request_id="legacy-sale-00000001", store="1号店",
                                       items=[SaleItem(sku_id=self.sku.id)]), self.manager, self.session)
        self.assertEqual(replay["order_no"], first["order_no"])
        self.assertIsNone(replay["payment_method"])


if __name__ == "__main__":
    unittest.main()

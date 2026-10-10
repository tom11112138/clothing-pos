"""Shared label metadata, confirmation guards and compatibility upgrades."""
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlmodel import Session, SQLModel, create_engine, select

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ADMIN"] = "false"

import database  # noqa: E402
from label_standards import label_issues, safety_text  # noqa: E402
from models import AuditEvent, Inventory, Product, Sku, User  # noqa: E402
from routers.products import create_product, router, update_product, update_product_label  # noqa: E402
from routers.skus import sku_retail_label, sku_retail_label_image  # noqa: E402
from schemas import ProductCreate, ProductLabelUpdate, ProductUpdate  # noqa: E402


def settings(**changes):
    return ProductLabelUpdate(expected_version=0, **{
        "composition": "100%棉", "execution_standard": "GB/T 22849-2024",
        "label_usage": "adult_skin", "safety_category": "B", "label_verified": True,
        **changes,
    })


class ProductLabelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            product = Product(code="TS001", name="纯棉圆领T恤", tag_price=199)
            manager = User(username="label_manager", password_hash="unused", role="manager")
            session.add_all([product, manager])
            session.flush()
            sku = Sku(product_id=product.id, barcode="TS001015", color="蓝", size="L", price=129)
            session.add(sku)
            session.flush()
            session.add(Inventory(sku_id=sku.id, store="1号店", quantity=7))
            session.commit()
            self.product_id, self.sku_id, self.manager_id = product.id, sku.id, manager.id

    def tearDown(self):
        self.engine.dispose()

    def test_unknown_details_allow_preview_but_not_formal_export(self):
        with Session(self.engine) as session:
            user = session.get(User, self.manager_id)
            preview = sku_retail_label(self.sku_id, "tag", user, session)
            self.assertFalse(preview["printable"])
            self.assertEqual(preview["composition"], "待填写")
            self.assertEqual(preview["safety_text"], "待确认")
            with self.assertRaises(HTTPException) as caught:
                sku_retail_label_image(self.sku_id, "tag", user, session)
            self.assertEqual(caught.exception.status_code, 400)

    def test_confirmed_details_are_shared_audited_and_do_not_touch_stock_or_barcode(self):
        with Session(self.engine) as session:
            manager = session.get(User, self.manager_id)
            product = update_product_label(self.product_id, settings(), manager, session)
            self.assertEqual(product.label_version, 1)
            self.assertTrue(product.label_verified)
            self.assertEqual(session.get(Sku, self.sku_id).barcode, "TS001015")
            self.assertEqual(session.exec(select(Inventory)).one().quantity, 7)
            self.assertEqual(session.exec(select(AuditEvent)).one().action, "product.label.update")
        with Session(self.engine) as session:
            data = sku_retail_label(self.sku_id, "tag", session.get(User, self.manager_id), session)
            self.assertTrue(data["printable"])
            self.assertEqual(data["execution_standard"], "GB/T 22849-2024")
            self.assertEqual(data["safety_text"], "GB 18401-2010 B类")

    def test_draft_can_be_saved_without_misrepresenting_it_as_confirmed(self):
        with Session(self.engine) as session:
            product = update_product_label(self.product_id, settings(composition=None, label_verified=False),
                                           session.get(User, self.manager_id), session)
            self.assertEqual(product.label_version, 1)
            self.assertFalse(product.label_verified)

    def test_product_category_updates_are_normalized_and_invalidate_old_confirmation(self):
        with Session(self.engine) as session:
            manager = session.get(User, self.manager_id)
            created = create_product(ProductCreate(code="DRESS", name="新款裙子", category=" 莲衣裙 "), manager, session)
            self.assertEqual(created.category, "连衣裙")
            update_product_label(self.product_id, settings(), manager, session)
            product = update_product(self.product_id, ProductUpdate(category=" 半袖 "), manager, session)
            self.assertEqual(product.category, "半袖")
            self.assertFalse(product.label_verified)
            self.assertEqual(product.label_version, 2)
            preview = sku_retail_label(self.sku_id, "tag", manager, session)
            self.assertEqual(preview["product_category"], "半袖")
            self.assertEqual(len(preview["standards"]["categories"]), 12)

    def test_catalog_route_precedes_product_id_route_and_requires_login(self):
        import asyncio
        import json
        from fastapi import Depends, FastAPI
        from security import get_current_user
        app = FastAPI()
        app.include_router(router, dependencies=[Depends(get_current_user)])

        async def request():
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            await app({"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                       "path": "/api/products/label-catalog", "root_path": "", "query_string": b"",
                       "headers": [], "server": ("test", 80), "client": ("test", 1234)}, receive, send)
            return messages[0]["status"], json.loads(b"".join(item.get("body", b"") for item in messages))

        self.assertEqual(asyncio.run(request())[0], 401)
        app.dependency_overrides[get_current_user] = lambda: User(username="staff", password_hash="unused", role="staff")
        status, body = asyncio.run(request())
        self.assertEqual(status, 200)
        self.assertEqual(len(body["categories"]), 12)
        self.assertEqual(len(body["execution_standards"]), 19)

    def test_incomplete_or_unsafe_confirmation_does_not_change_metadata(self):
        for changes in ({"composition": None}, {"safety_category": "C"}, {"execution_standard": ""}):
            with self.subTest(changes=changes), Session(self.engine) as session:
                with self.assertRaises(HTTPException) as caught:
                    update_product_label(self.product_id, settings(**changes), session.get(User, self.manager_id), session)
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(session.get(Product, self.product_id).label_version, 0)
                self.assertIsNone(session.get(Product, self.product_id).composition)
                self.assertEqual(session.exec(select(AuditEvent)).all(), [])

    def test_stale_save_rejects_overwriting_another_administrators_settings(self):
        with Session(self.engine) as session:
            manager = session.get(User, self.manager_id)
            update_product_label(self.product_id, settings(), manager, session)
            with self.assertRaises(HTTPException) as caught:
                update_product_label(self.product_id, settings(composition="50%棉 50%聚酯纤维"), manager, session)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(session.get(Product, self.product_id).composition, "100%棉")
            self.assertEqual(len(session.exec(select(AuditEvent)).all()), 1)

    def test_product_identity_change_invalidates_previous_confirmation(self):
        with Session(self.engine) as session:
            manager = session.get(User, self.manager_id)
            update_product_label(self.product_id, settings(), manager, session)
            product = update_product(self.product_id, ProductUpdate(name="另一款服装"), manager, session)
            self.assertFalse(product.label_verified)
            self.assertEqual(product.label_version, 2)
            self.assertFalse(sku_retail_label(self.sku_id, "tag", manager, session)["printable"])

    def test_failed_commit_rolls_back_metadata_and_audit_together(self):
        with Session(self.engine) as session:
            with patch.object(session, "commit", side_effect=RuntimeError("commit failed")), self.assertRaises(RuntimeError):
                update_product_label(self.product_id, settings(), session.get(User, self.manager_id), session)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Product, self.product_id).label_version, 0)
            self.assertEqual(session.exec(select(AuditEvent)).all(), [])

    def test_label_writer_locks_and_refreshes_product_before_validation(self):
        with Session(self.engine) as session:
            statements, execute = [], session.exec
            def record(statement, *args, **kwargs):
                statements.append(statement)
                return execute(statement, *args, **kwargs)
            manager = session.get(User, self.manager_id)
            stale = settings()
            stale.expected_version = 99
            with patch.object(session, "exec", side_effect=record), self.assertRaises(HTTPException):
                update_product_label(self.product_id, stale, manager, session)
            self.assertIn("FOR UPDATE", str(statements[0].compile(dialect=postgresql.dialect())))
            self.assertTrue(statements[0].get_execution_options()["populate_existing"])

    def test_age_and_contact_guards_follow_safety_categories_not_product_name(self):
        config = settings().model_dump()
        config["safety_category"] = "A"
        self.assertEqual(label_issues(config), [])
        config.update(label_usage="adult_outer", safety_category="C")
        self.assertEqual(label_issues(config), [])
        config.update(label_usage="child_skin", safety_category="B")
        self.assertEqual(safety_text(config), "GB 31701-2015 B类")
        config.update(label_usage="infant", safety_category="B")
        self.assertTrue(any("至少需要A类" in issue for issue in label_issues(config)))
        config.update(label_usage="infant", safety_category="A", execution_standard="fz/t81006—2017")
        self.assertTrue(any("不适用于" in issue for issue in label_issues(config)))

    def test_old_database_upgrade_is_repeatable_and_preserves_existing_products(self):
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                connection.execute(text("create table product (id integer primary key, code varchar, name varchar, "
                                        "category varchar, brand varchar, tag_price float, image_url varchar, created_at datetime)"))
                connection.execute(text("insert into product (id, code, name, tag_price) values (1, 'KEEP', 'Original', 199)"))
            SQLModel.metadata.create_all(engine)
            with patch.object(database, "engine", engine):
                database._ensure_runtime_schema()
                database._ensure_runtime_schema()
            with engine.begin() as connection:
                row = connection.execute(text("select code, name, tag_price, composition, label_verified, label_version from product")).one()
                self.assertEqual(tuple(row), ("KEEP", "Original", 199, None, 0, 0))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()

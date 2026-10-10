"""Label content, geometry and scan-critical barcode checks on an isolated database."""
import base64
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ADMIN"] = "false"

from label_utils import (DPI, HEIGHT, MARGIN, WIDTH, barcode_geometry,
                         generate_label_png, label_data)  # noqa: E402
from models import Product, Sku, User  # noqa: E402
from routers.skus import sku_retail_label, sku_retail_label_image  # noqa: E402


def example():
    return label_data(
        SimpleNamespace(id=1, color="蓝", size="L", barcode="TS001015", price=129),
        SimpleNamespace(name="纯棉圆领T恤", code="TS001", brand=None, tag_price=0,
                        composition="100%棉", execution_standard="GB/T 22849-2024",
                        label_usage="adult_skin", safety_category="B", label_verified=True),
    )


class LabelTests(unittest.TestCase):
    def test_price_basis_uses_known_prices_without_fabricating_a_tag_price(self):
        sku = SimpleNamespace(id=1, color="蓝", size="L", barcode="TS001015", price=129)
        product = SimpleNamespace(name="T shirt", code="TS001", brand="BRAND", tag_price=199)
        self.assertEqual(label_data(sku, product)["price"], "199.00")
        self.assertEqual(label_data(sku, product)["price_title"], "建议零售价")
        self.assertEqual(label_data(sku, product, "selling")["price"], "129.00")
        product.tag_price = 0
        self.assertEqual(label_data(sku, product)["price_title"], "售价")
        self.assertNotIn("cost", label_data(sku, product))
        for value in (float("nan"), float("inf"), -1, 1e99):
            sku.price = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                label_data(sku, product)

    def test_monochrome_image_has_exact_40_by_60_mm_b21_geometry(self):
        image = Image.open(io.BytesIO(generate_label_png(example())))
        self.assertEqual(image.size, (320, 480))
        self.assertEqual(image.mode, "1")
        self.assertAlmostEqual(image.info["dpi"][0], DPI, places=1)
        self.assertTrue(image.crop((0, 0, MARGIN, HEIGHT)).convert("L").getextrema() == (255, 255))
        self.assertTrue(image.crop((WIDTH - MARGIN, 0, WIDTH, HEIGHT)).convert("L").getextrema() == (255, 255))

    def test_printed_barcode_matches_encoder_bits_and_has_full_quiet_zones(self):
        data = example()
        image = Image.open(io.BytesIO(generate_label_png(data))).convert("L")
        bits, module = barcode_geometry(data["barcode"])
        left = (WIDTH - len(bits) * module) // 2
        self.assertGreaterEqual(module, 2)
        self.assertGreaterEqual(left, module * 10)
        self.assertGreaterEqual(WIDTH - left - len(bits) * module, module * 10)
        for index, bit in enumerate(bits):
            for x in range(left + index * module, left + (index + 1) * module):
                self.assertEqual(image.getpixel((x, 390)), 0 if bit == "1" else 255)
        self.assertEqual(image.crop((0, 352, left, 428)).getextrema(), (255, 255))
        self.assertEqual(image.crop((left + len(bits) * module, 352, WIDTH, 428)).getextrema(), (255, 255))

    def test_oversize_text_and_barcodes_are_rejected_instead_of_silently_clipped(self):
        for field, value in (("product_name", "很长的商品名称" * 30),
                             ("product_code", "CODE" * 40),
                             ("color", "深蓝色" * 40),
                             ("barcode", "ABCDEFG" * 12),
                             ("barcode", ""), ("barcode", "中文条码")):
            data = example()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                generate_label_png(data)

    def test_reference_layout_contains_declared_details_and_never_a_pants_type(self):
        written, original = [], ImageDraw.ImageDraw.text
        def record(draw, position, text, *args, **kwargs):
            written.append(text)
            return original(draw, position, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", new=record):
            generate_label_png(example())
        for text in ("合格证", "品名：纯棉圆领T恤", "款号：TS001", "颜色：蓝", "规格：", "L",
                     "成份：100%棉", "执行标准：GB/T 22849-2024", "安全技术类别：GB 18401-2010 B类",
                     "TS001015", "售价：129.00元"):
            self.assertIn(text, written)
        self.assertFalse(any("裤型" in text for text in written))

    def test_draft_and_sample_do_not_render_an_unqualified_certificate_title(self):
        written, original = [], ImageDraw.ImageDraw.text
        def record(draw, position, text, *args, **kwargs):
            written.append(text)
            return original(draw, position, text, *args, **kwargs)
        data = example()
        data["printable"] = False
        with patch.object(ImageDraw.ImageDraw, "text", new=record):
            generate_label_png(data)
            data["sample"] = True
            generate_label_png(data)
        self.assertIn("合格证预览", written)
        self.assertIn("合格证（样板）", written)
        self.assertNotIn("合格证", written)

    def test_authenticated_label_routes_preserve_sku_and_export_valid_png(self):
        engine = create_engine("sqlite://")
        try:
            SQLModel.metadata.create_all(engine)
            with Session(engine) as session:
                product = Product(code="TS001", name="纯棉圆领T恤", brand="示例品牌", tag_price=199,
                                  composition="100%棉", execution_standard="GB/T 22849-2024",
                                  label_usage="adult_skin", safety_category="B", label_verified=True)
                user = User(username="label_test", password_hash="unused", role="staff")
                session.add_all([product, user])
                session.flush()
                sku = Sku(product_id=product.id, barcode="TS001015", color="蓝", size="L", price=129, cost=30)
                session.add(sku)
                session.commit()
                data = sku_retail_label(sku.id, "selling", user, session)
                self.assertEqual(data["barcode"], "TS001015")
                self.assertEqual(data["price"], "129.00")
                self.assertNotIn("cost", data)
                self.assertEqual(Image.open(io.BytesIO(base64.b64decode(data["image_url"].split(",")[1]))).size, (320, 480))
                export = sku_retail_label_image(sku.id, "tag", user, session)
                self.assertEqual(export.media_type, "image/png")
                self.assertIn("40x60mm.png", export.headers["content-disposition"])
                self.assertEqual(export.headers["cache-control"], "no-store")
                with self.assertRaises(HTTPException) as caught:
                    sku_retail_label(99999, "tag", user, session)
                self.assertEqual(caught.exception.status_code, 404)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()

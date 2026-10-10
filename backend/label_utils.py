"""Pixel-aligned retail stickers for the 203 dpi NIIMBOT B21."""
import io
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import barcode
from PIL import Image, ImageDraw, ImageFont
from label_standards import label_configuration, label_issues, normalize_category, safety_text

DPI = 203
WIDTH_MM, HEIGHT_MM = 40, 60
WIDTH, HEIGHT = round(WIDTH_MM * DPI / 25.4), round(HEIGHT_MM * DPI / 25.4)
MARGIN = 16


def _font(size: int, *, bold: bool = False):
    configured = os.getenv("LABEL_FONT_PATH")
    candidates = [configured] if configured else [
        str(Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts" / ("msyhbd.ttc" if bold else "msyh.ttc")),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError as exc:
                raise RuntimeError("标签字体无法读取，请检查 LABEL_FONT_PATH") from exc
    raise RuntimeError("缺少中文标签字体，请配置 LABEL_FONT_PATH 或安装 Noto Sans CJK 字体")


def _text(value):
    return " ".join(str(value or "").split())


def label_data(sku, product, price_basis: str = "tag") -> dict:
    if price_basis not in {"tag", "selling"}:
        raise ValueError("价格类型不正确")
    tagged = price_basis == "tag" and product.tag_price > 0
    amount = Decimal(str(product.tag_price if tagged else sku.price))
    if not amount.is_finite() or amount < 0:
        raise ValueError("商品价格无效，请先修正价格")
    try:
        price = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError("商品价格超出标签可显示范围") from exc
    config = label_configuration(product)
    issues = label_issues(config)
    if not _text(product.name) or not _text(product.code):
        issues.append("请补充商品品名和款号")
    if not _text(sku.color) or not _text(sku.size):
        issues.append("请补充该 SKU 的颜色和规格")
    return {
        "sku_id": sku.id, "product_id": getattr(product, "id", None), "product_name": _text(product.name),
        "product_code": _text(product.code), "brand": _text(product.brand),
        "product_category": normalize_category(getattr(product, "category", None)),
        "color": _text(sku.color) or "-", "size": _text(sku.size) or "-",
        "barcode": sku.barcode, "price_basis": price_basis,
        "price_title": "建议零售价" if tagged else "售价",
        "price": price,
        "composition": _text(config["composition"]) or "待填写",
        "execution_standard": _text(config["execution_standard"]) or "待确认",
        "safety_text": safety_text(config), "infant": config["label_usage"] == "infant",
        "configuration": config, "label_version": getattr(product, "label_version", 0),
        "printable": not issues, "issues": issues,
        "width_mm": WIDTH_MM, "height_mm": HEIGHT_MM, "dpi": DPI,
        "width_px": WIDTH, "height_px": HEIGHT,
    }


def _fitted_font(draw, text, width, start, minimum=14, *, bold=False):
    for size in range(start, minimum - 1, -1):
        font = _font(size, bold=bold)
        if draw.textlength(text, font=font) <= width:
            return font
    raise ValueError("标签文字过长，40 mm 标签无法完整容纳，请缩短商品名称或规格信息")


def _wrapped_text(draw, text, width, *, start=20, minimum=16, max_lines=2, prefer_words=False):
    for size in range(start, minimum - 1, -1):
        font = _font(size)
        lines, line = [], ""
        for char in text:
            if line and draw.textlength(line + char, font=font) > width:
                split = line.rfind(" ") if prefer_words else -1
                if split > 0:
                    lines.append(line[:split])
                    line = line[split + 1:] + char
                    continue
                lines.append(line)
                line = ""
            line += char
        if line:
            lines.append(line)
        if 0 < len(lines) <= max_lines and all(draw.textlength(line, font=font) <= width for line in lines):
            return font, lines
    raise ValueError("品名或成份过长，40 mm 标签无法完整容纳，请先整理标签资料")


def _center(draw, text, y, font):
    draw.text(((WIDTH - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=0, anchor="lt")


def barcode_geometry(code: str) -> tuple[str, int]:
    if not isinstance(code, str) or not code:
        raise ValueError("商品条码不能为空")
    try:
        bits = barcode.get("code128", code).build()[0]
    except (ValueError, barcode.errors.BarcodeError) as exc:
        raise ValueError("条码必须是 Code128 支持的字符，请检查商品条码") from exc
    # Ten quiet modules on each side; never shrink a narrow bar below two dots.
    module = (WIDTH - 2 * MARGIN) // (len(bits) + 20)
    if module < 2:
        raise ValueError("条码过长，40 mm 标签无法保证清晰扫码，请使用更短的商品条码或更宽标签")
    return bits, module


def generate_label_png(data: dict) -> bytes:
    image = Image.new("1", (WIDTH, HEIGHT), 1)
    draw = ImageDraw.Draw(image)
    available = WIDTH - 2 * MARGIN
    heading = "合格证（样板）" if data.get("sample") else ("合格证" if data["printable"] else "合格证预览")
    _center(draw, heading, 17, _fitted_font(draw, heading, available, 26, bold=True))
    if data["infant"]:
        _center(draw, "婴幼儿用品", 46, _font(13))
    font, lines = _wrapped_text(draw, "品名：" + data["product_name"], available)
    for index, line in enumerate(lines):
        draw.text((MARGIN, 64 + index * 22), line, font=font, fill=0, anchor="lt")
    for title, value, y, size in (
        ("款号", data["product_code"], 112, 20),
        ("颜色", data["color"], 140, 20),
    ):
        text = f"{title}：{value}"
        draw.text((MARGIN, y), text, fill=0, anchor="lt",
                  font=_fitted_font(draw, text, available, size))
    draw.text((MARGIN, 174), "规格：", font=_font(20), fill=0, anchor="lt")
    draw.text((MARGIN + 70, 166), data["size"], fill=0, anchor="lt",
              font=_fitted_font(draw, data["size"], available - 70, 32, bold=True))
    font, lines = _wrapped_text(draw, "成份：" + data["composition"], available,
                               start=18, minimum=14, max_lines=3, prefer_words=True)
    for index, line in enumerate(lines):
        draw.text((MARGIN, 210 + index * 21), line, font=font, fill=0, anchor="lt")
    for text, y in (("执行标准：" + data["execution_standard"], 283),
                    ("安全技术类别：" + data["safety_text"], 312)):
        draw.text((MARGIN, y), text, fill=0, anchor="lt",
                  font=_fitted_font(draw, text, available, 15))
    bits, module = barcode_geometry(data["barcode"])
    left = (WIDTH - len(bits) * module) // 2
    for index, bit in enumerate(bits):
        if bit == "1":
            x = left + index * module
            draw.rectangle((x, 345, x + module - 1, 408), fill=0)
    _center(draw, data["barcode"], 418,
            _fitted_font(draw, data["barcode"], available, 17, minimum=12))
    price = data["price_title"] + "：" + data["price"] + "元"
    _center(draw, price, 451, _fitted_font(draw, price, available, 18))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", dpi=(DPI, DPI))
    return buffer.getvalue()

import io

import barcode
from barcode.writer import ImageWriter


def generate_barcode_png(code: str) -> bytes:
    """用 Code128 生成条形码 PNG 字节流。

    Code128 支持字母+数字，适合自定义店内 SKU 编码；
    如果将来要对接标准零售供应链，再换成 EAN-13（需正规厂商识别码）。
    """
    code128 = barcode.get("code128", code, writer=ImageWriter())
    buffer = io.BytesIO()
    code128.write(
        buffer,
        options={
            "module_width": 0.2,
            "module_height": 12.0,
            "font_size": 8,
            "text_distance": 3.0,
            "quiet_zone": 2.0,
        },
    )
    return buffer.getvalue()


def make_sku_barcode(product_code: str, seq: int) -> str:
    """缺省条码编码规则：款号 + 3 位序号。

    例：款号 TS001 的第 1 个 SKU -> TS001001。
    规则可自定义，比如 品类码 + 款号 + 颜色码 + 尺码码。
    若你的扫描枪较老、只认数字，可把 product_code 也设计成纯数字。
    """
    return f"{product_code}{seq:03d}"

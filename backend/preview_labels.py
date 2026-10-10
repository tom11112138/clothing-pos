"""Generate a clearly marked reference-layout sample without reading store data."""
import argparse
from pathlib import Path
from types import SimpleNamespace

from label_utils import generate_label_png, label_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = label_data(
        SimpleNamespace(id=1, color="蓝色", size="30", barcode="26151030", price=593),
        SimpleNamespace(name="休闲女裤", code="26151", brand=None, tag_price=593,
                        composition="68%棉 30.2%聚酯纤维 1.8%氨纶",
                        execution_standard="FZ/T 81006-2017", label_usage="adult_skin",
                        safety_category="B", label_verified=False),
    )
    data["sample"] = True
    args.output.write_bytes(generate_label_png(data))
    print(f"Sample label: {args.output.resolve()} ({data['width_px']}x{data['height_px']} at {data['dpi']} dpi)")


if __name__ == "__main__":
    main()

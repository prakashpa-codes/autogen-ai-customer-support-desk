"""Generate knowledge_base/product_catalogue.md from data/catalogue.json.

The JSON catalogue is the single source of truth: the storefront renders it directly and
this script turns it into a knowledge-base document so agents can answer product questions.

Usage:  python scripts/build_catalogue_kb.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE = ROOT / "data" / "catalogue.json"
TARGET = ROOT / "knowledge_base" / "product_catalogue.md"


def rupees(amount: int) -> str:
    # Indian digit grouping: 1,00,000 style
    s = str(amount)
    if len(s) <= 3:
        return f"₹{s}"
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"₹{','.join(groups)},{tail}"


def render(catalogue: dict) -> str:
    lines = [
        "# Product Catalogue",
        "",
        f"Dada Sports product range with current prices (updated {catalogue['updated']}). "
        "Prices include GST. Return windows and warranties are summarised per product; "
        "the Returns and Refund Policy and the Warranty and Replacements policy give the full rules.",
        "",
    ]
    for category in catalogue["categories"]:
        products = [p for p in catalogue["products"] if p["category"] == category["id"]]
        lines += [f"## {category['name']}", ""]
        for p in products:
            returns = (
                f"{p['return_window_days']}-day return window"
                if p["returnable"]
                else "Not returnable"
            )
            stock = "In stock" if p["in_stock"] else "Currently out of stock"
            lines += [
                f"### {p['name']} ({p['sku']})",
                "",
                f"- Price: {rupees(p['price'])} (MRP {rupees(p['mrp'])}). {stock}.",
                f"- {p['description']}",
                *[f"- {h}" for h in p["highlights"]],
                f"- Warranty: {p['warranty']}. {returns}.",
                f"- Customer rating: {p['rating']} / 5 from {p['reviews']} reviews.",
                "",
            ]
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    catalogue = json.loads(SOURCE.read_text(encoding="utf-8"))
    TARGET.write_text(render(catalogue), encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)} ({len(catalogue['products'])} products)")


if __name__ == "__main__":
    main()

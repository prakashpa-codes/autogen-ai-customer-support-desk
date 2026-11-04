"""Render the knowledge base into one PDF: docs/Dada_Sports_Support_Handbook.pdf.

The Markdown files in knowledge_base/ stay the source of truth (they are what the agents
search). This handbook is the same content for people: support staff onboarding, audits,
or sharing policies outside the system.

Usage:  python scripts/build_handbook_pdf.py
Needs a Unicode TTF font for the rupee sign: Arial on Windows, DejaVu Sans on Linux, or set
FONT_PATH / FONT_BOLD_PATH.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

ROOT = Path(__file__).resolve().parents[1]
KB_DIR = ROOT / "knowledge_base"
OUTPUT = ROOT / "docs" / "Dada_Sports_Support_Handbook.pdf"

# Policies first, then reference material.
ORDER = [
    "about_dada_sports.md",
    "shipping_policy.md",
    "orders_and_cancellations.md",
    "refund_policy.md",
    "warranty_and_replacements.md",
    "billing_and_payments.md",
    "account_and_login.md",
    "bat_care_and_services.md",
    "team_and_academy_orders.md",
    "product_faq.md",
    "resolved_cases.md",
    "product_catalogue.md",
]
NAVY, GOLD, INK, MUTED = (11, 37, 69), (224, 165, 38), (27, 27, 26), (107, 106, 101)
FONT_CANDIDATES = [
    (os.getenv("FONT_PATH"), os.getenv("FONT_BOLD_PATH")),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


class Handbook(FPDF):
    def header(self) -> None:
        if self.page_no() > 1:
            self.set_font("body", "", 8)
            self.set_text_color(*MUTED)
            self.cell(0, 6, "Dada Sports · Customer Support Handbook", align="L")
            self.ln(10)

    def footer(self) -> None:
        if self.page_no() > 1:
            self.set_y(-14)
            self.set_font("body", "", 8)
            self.set_text_color(*MUTED)
            self.cell(0, 8, f"Page {self.page_no()}", align="R")


def _font_paths() -> tuple[str, str]:
    for regular, bold in FONT_CANDIDATES:
        if regular and bold and Path(regular).exists() and Path(bold).exists():
            return regular, bold
    sys.exit("No Unicode TTF font found. Set FONT_PATH and FONT_BOLD_PATH.")


def _inline(text: str) -> str:
    """fpdf2 markdown supports **bold**; drop other Markdown syntax."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.replace("__", "")


def _table(pdf: Handbook, lines: list[str]) -> None:
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines if not re.match(r"^\|?\s*-", ln.strip("| "))]
    pdf.set_font("body", "", 8.5)
    pdf.set_text_color(*INK)
    # Set colours explicitly: tables otherwise inherit the cover fill and the heading rule colour.
    pdf.set_fill_color(255, 255, 255)
    pdf.set_draw_color(225, 224, 217)
    pdf.set_line_width(0.2)
    with pdf.table(
        headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=NAVY),
        cell_fill_color=(246, 244, 238),
        cell_fill_mode="ROWS",
        line_height=5,
        padding=1.5,
        text_align="LEFT",
        borders_layout="HORIZONTAL_LINES",
    ) as table:
        for row in rows:
            r = table.row()
            for cell in row:
                r.cell(_inline(cell))
    pdf.ln(3)


def render_document(pdf: Handbook, path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    pdf.add_page()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("# "):
            pdf.set_font("body", "B", 20)
            pdf.set_text_color(*NAVY)
            pdf.multi_cell(0, 10, line[2:].strip())
            pdf.set_draw_color(*GOLD)
            pdf.set_line_width(0.8)
            pdf.line(pdf.l_margin, pdf.get_y() + 1, pdf.l_margin + 30, pdf.get_y() + 1)
            pdf.ln(5)
        elif line.startswith("## "):
            pdf.ln(2)
            pdf.set_font("body", "B", 13)
            pdf.set_text_color(*NAVY)
            pdf.multi_cell(0, 7, line[3:].strip())
            pdf.ln(1)
        elif line.startswith("### "):
            pdf.set_font("body", "B", 10.5)
            pdf.set_text_color(*INK)
            pdf.multi_cell(0, 6, line[4:].strip())
        elif line.lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            _table(pdf, block)
            continue
        elif re.match(r"^\s*(-|\d+\.)\s+", line):
            marker, text = re.match(r"^\s*(-|\d+\.)\s+(.*)", line).groups()
            pdf.set_font("body", "", 9.5)
            pdf.set_text_color(*INK)
            pdf.set_x(pdf.l_margin + 3)
            pdf.multi_cell(0, 5, f"{'•' if marker == '-' else marker} {_inline(text)}", markdown=True)
        elif line.strip():
            pdf.set_font("body", "", 9.5)
            pdf.set_text_color(*INK)
            pdf.multi_cell(0, 5, _inline(line.strip()), markdown=True)
            pdf.ln(1.5)
        else:
            pdf.ln(1.5)
        i += 1


def main() -> None:
    regular, bold = _font_paths()
    pdf = Handbook(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 16, 18)
    pdf.add_font("body", "", regular)
    pdf.add_font("body", "B", bold)

    # Cover
    pdf.add_page()
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 297, "F")
    pdf.set_y(95)
    pdf.set_font("body", "B", 34)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 16, "DADA SPORTS", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("body", "", 15)
    pdf.set_text_color(*GOLD)
    pdf.cell(0, 10, "Customer Support Handbook", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("body", "", 10)
    pdf.set_text_color(220, 226, 236)
    pdf.cell(0, 6, f"Policies, procedures and product reference · {date.today():%B %Y}", align="C")

    for name in ORDER + sorted(p.name for p in KB_DIR.glob("*.md") if p.name not in ORDER):
        render_document(pdf, KB_DIR / name)

    OUTPUT.parent.mkdir(exist_ok=True)
    pdf.output(str(OUTPUT))
    print(f"Wrote {OUTPUT.relative_to(ROOT)} ({pdf.page_no()} pages)")


if __name__ == "__main__":
    main()

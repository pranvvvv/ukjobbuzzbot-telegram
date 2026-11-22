"""Regenerate binary test fixtures (synthetic resumes only; never real people)."""

from __future__ import annotations

import io
from pathlib import Path

import docx
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "resumes"


def text_pdf(src: Path, dest: Path) -> None:
    c = canvas.Canvas(str(dest), pagesize=A4)
    y = 800
    for line in src.read_text(encoding="utf-8").splitlines():
        c.drawString(50, y, line)
        y -= 16
        if y < 50:
            c.showPage()
            y = 800
    c.save()


def scanned_pdf(dest: Path) -> None:
    img = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(img)
    for i in range(25):
        draw.text((80, 80 + i * 50), "Scanned resume line (pixels, not text)", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    c = canvas.Canvas(str(dest), pagesize=A4)
    c.drawImage(ImageReader(buf), 0, 0, width=A4[0], height=A4[1])
    c.save()


def table_docx(dest: Path) -> None:
    d = docx.Document()
    d.add_paragraph("Priya Nair")
    table = d.add_table(rows=4, cols=2)
    cells = [
        ("Contact", "Bengaluru | priya.nair@example.com | +91 91234 56789"),
        (
            "Experience",
            "Data Analyst, Flipkart | Jan 2022 - Present\n"
            "Built Power BI dashboards and SQL pipelines; Python and Pandas automation",
        ),
        ("Previous", "Analyst Intern, Swiggy | Jun 2021 - Dec 2021\nExcel reporting"),
        ("Education", "B.Sc Statistics, Christ University, 2021"),
    ]
    for row, (left, right) in zip(table.rows, cells, strict=True):
        row.cells[0].text = left
        row.cells[1].text = right
    d.add_paragraph("Skills: SQL, Python, Pandas, Power BI, Tableau, Excel, Statistics")
    d.save(str(dest))


if __name__ == "__main__":
    text_pdf(OUT / "fresher_backend.txt", OUT / "fresher_backend.pdf")
    scanned_pdf(OUT / "scanned.pdf")
    table_docx(OUT / "mid_data_tables.docx")
    print("fixtures written to", OUT)

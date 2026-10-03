"""網頁版 pdf2md（pdf2md/web/pdf2md-core.js）與 Python 版的對拍測試。

同一批程式產生的 PDF 同時交給兩邊轉換，比對標題、段落、清單與無框線表格。
需要 node 與 pdf.js（npm install --prefix pdf2md/web）；缺少時整檔跳過。
網頁版不做有框線表格與圖片，那兩項只測 Python 版。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from pdf2md import convert
from test_pdf2md import _journal_pdf, build_sample_pdf, font_files  # noqa: F401  (font_files 是 fixture)

WEB = Path(__file__).resolve().parent.parent / "pdf2md" / "web"
CLI = WEB / "cli.js"

if shutil.which("node") is None or not (WEB / "node_modules" / "pdfjs-dist").is_dir():
    pytest.skip("需要 node 與 pdf.js：npm install --prefix pdf2md/web", allow_module_level=True)


def run_js(path, **opts):
    proc = subprocess.run(
        ["node", str(CLI), str(path), json.dumps(opts)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def headings(md):
    return [line for line in md.splitlines() if line.startswith("#")]


def table_rows(md):
    return [line for line in md.splitlines() if line.startswith("|")]


@pytest.fixture
def sample_pdf(tmp_path):
    return build_sample_pdf(tmp_path / "sample.pdf")


def test_sample_headings_match_python(sample_pdf):
    js = run_js(sample_pdf)["markdown"]
    assert headings(js) == headings(convert(sample_pdf).markdown)


def test_sample_structure(sample_pdf):
    md = run_js(sample_pdf)["markdown"]
    assert ("Hyponatremia is the most common electrolyte disorder in hospitalized patients "
            "and is associated with increased morbidity and mortality.") in md
    assert "低血鈉是住院病人最常見的電解質異常，需依血漿滲透壓分層鑑別。" in md
    assert "- Check plasma osmolality\n- Assess volume status\n- Measure urine sodium" in md
    assert "```\ndelta = (inf_na - s_na) / (tbw + 1)\n```" in md
    assert "This sentence continues on the next page without a break." in md
    assert "Clinical Handbook" not in md and "Page 1 of 3" not in md


def test_keep_headers_and_page_selection(sample_pdf):
    kept = run_js(sample_pdf, keepHeaders=True)["markdown"]
    assert "Clinical Handbook 2026" in kept
    page2 = run_js(sample_pdf, pages="2", pageBreaks=True)["markdown"]
    assert page2.startswith("<!-- page 2 -->")
    assert "Sodium Disorders" not in page2 and "Formula" in page2


def test_journal_layout_matches_python(tmp_path, font_files):
    path = _journal_pdf(tmp_path / "j.pdf", font_files)
    js, py = run_js(path)["markdown"], convert(path).markdown
    assert headings(js) == headings(py) == ["# Introduction", "# Methods", "## Question 1: should fans be used?"]
    assert table_rows(js) == table_rows(py)
    assert "\nGET: graded exercise therapy; footnote" in js


def test_rotated_table_matches_python(tmp_path, font_files):
    src = _journal_pdf(tmp_path / "j.pdf", font_files)
    out = pymupdf.open()
    page = out.new_page(width=842, height=595)
    page.show_pdf_page(page.rect, pymupdf.open(src), 0, rotate=90)
    path = tmp_path / "rot.pdf"
    out.save(path)
    js, py = run_js(path)["markdown"], convert(path).markdown
    assert table_rows(js) == table_rows(py)
    assert len(table_rows(js)) == 4


def test_scanned_page_warning(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    page.insert_image(page.rect, pixmap=pix)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    assert any("OCR" in w for w in run_js(path)["warnings"])


def test_encrypted_pdf(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 80), "Secret note", fontname="helv", fontsize=11)
    path = tmp_path / "locked.pdf"
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    with pytest.raises(RuntimeError):
        run_js(path)
    assert "Secret note" in run_js(path, password="pw")["markdown"]

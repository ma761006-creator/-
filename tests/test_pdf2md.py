from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from pdf2md import convert
from pdf2md.__main__ import main
from pdf2md.converter import join_lines, parse_page_spec


# --------------------------------------------------------------------------
# 測試用 PDF：3 頁，含頁首頁尾、各級標題、中英文段落、清單、表格、程式碼、圖片
# --------------------------------------------------------------------------
def _draw_table(page, x, y, rows, col_w=120, row_h=22):
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(x + c * col_w, y + r * row_h, x + (c + 1) * col_w, y + (r + 1) * row_h)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((rect.x0 + 4, rect.y1 - 7), cell, fontname="helv", fontsize=10)


def build_sample_pdf(path: Path, *, with_image: bool = True) -> Path:
    doc = pymupdf.open()
    for n in range(1, 4):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 30), "Clinical Handbook 2026", fontname="helv", fontsize=9)
        page.insert_text((280, 825), f"Page {n} of 3", fontname="helv", fontsize=9)

        if n == 1:
            page.insert_text((50, 90), "Sodium Disorders", fontname="hebo", fontsize=24)
            page.insert_text((50, 130), "Overview", fontname="hebo", fontsize=16)
            page.insert_textbox(
                pymupdf.Rect(50, 140, 545, 200),
                "Hyponatremia is the most common electrolyte disorder in hospitalized "
                "patients and is associated with increased morbidity and mortality.",
                fontname="helv", fontsize=11,
            )
            page.insert_textbox(
                pymupdf.Rect(50, 205, 200, 300),
                "低血鈉是住院病人最常見的電解質異常，需依血漿滲透壓分層鑑別。",
                fontname="china-t", fontsize=11,
            )
            y = 320
            for item in ["Check plasma osmolality", "Assess volume status", "Measure urine sodium"]:
                page.insert_text((60, y), "•", fontname="helv", fontsize=11)
                page.insert_text((75, y), item, fontname="helv", fontsize=11)
                y += 16
            page.insert_text((50, 400), "Key Thresholds", fontname="hebo", fontsize=11)
            _draw_table(page, 50, 415, [["Parameter", "Cutoff", "Meaning"],
                                        ["U_Na", "20", "Renal loss"],
                                        ["U_osm", "100", "Water intake"]])
            if with_image:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
                pix.set_rect(pix.irect, (200, 30, 30))
                page.insert_image(pymupdf.Rect(50, 520, 114, 584), pixmap=pix)
        elif n == 2:
            page.insert_text((50, 90), "Formula", fontname="hebo", fontsize=16)
            page.insert_text((50, 120), "delta = (inf_na - s_na) / (tbw + 1)", fontname="cour", fontsize=10)
            page.insert_textbox(
                pymupdf.Rect(50, 760, 545, 800),
                "This sentence continues on the next",
                fontname="helv", fontsize=11,
            )
        else:
            page.insert_textbox(
                pymupdf.Rect(50, 80, 545, 120),
                "page without a break.",
                fontname="helv", fontsize=11,
            )
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def sample_pdf(tmp_path):
    return build_sample_pdf(tmp_path / "sample.pdf")


def test_headings_by_font_size(sample_pdf):
    md = convert(sample_pdf).markdown
    assert "# Sodium Disorders" in md.splitlines()
    assert "## Overview" in md.splitlines()
    assert "## Formula" in md.splitlines()
    # 內文字級、整行粗體 → 最低一級標題
    assert "### Key Thresholds" in md.splitlines()


def test_paragraph_lines_are_joined(sample_pdf):
    md = convert(sample_pdf).markdown
    assert ("Hyponatremia is the most common electrolyte disorder in hospitalized patients "
            "and is associated with increased morbidity and mortality.") in md
    # 中文換行不應插入空格
    assert "低血鈉是住院病人最常見的電解質異常，需依血漿滲透壓分層鑑別。" in md


def test_paragraph_continues_across_pages(sample_pdf):
    md = convert(sample_pdf).markdown
    assert "This sentence continues on the next page without a break." in md


def test_bullets(sample_pdf):
    md = convert(sample_pdf).markdown
    assert "- Check plasma osmolality\n- Assess volume status\n- Measure urine sodium" in md


def test_table(sample_pdf):
    md = convert(sample_pdf).markdown
    assert "| Parameter | Cutoff | Meaning |" in md
    assert "| U_Na | 20 | Renal loss |" in md
    # 表格內文字不重複輸出成段落
    assert md.count("Renal loss") == 1


def test_code_block(sample_pdf):
    md = convert(sample_pdf).markdown
    assert "```\ndelta = (inf_na - s_na) / (tbw + 1)\n```" in md


def test_headers_footers_removed_by_default(sample_pdf):
    md = convert(sample_pdf).markdown
    assert "Clinical Handbook" not in md
    assert "Page 1 of 3" not in md

    kept = convert(sample_pdf, keep_headers=True).markdown
    assert "Clinical Handbook 2026" in kept
    assert "Page 2 of 3" in kept


def test_two_page_header_removed(tmp_path):
    doc = pymupdf.open()
    for n in (1, 2):
        page = doc.new_page()
        page.insert_text((50, 30), "衛教資料", fontname="china-t", fontsize=8)
        page.insert_text((50, 100), f"Body text {n}.", fontname="helv", fontsize=11)
        page.insert_text((280, 830), f"- {n} -", fontname="helv", fontsize=8)
    path = tmp_path / "two.pdf"
    doc.save(path)
    md = convert(path).markdown
    assert "衛教資料" not in md and "- 1 -" not in md
    assert "Body text 1." in md and "Body text 2." in md


def test_bullet_and_ordered_lists_are_separate(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    for y, text in [(100, "- first"), (116, "- second"), (132, "1. step one"), (148, "2. step two")]:
        page.insert_text((60, y), text, fontname="helv", fontsize=11)
    path = tmp_path / "lists.pdf"
    doc.save(path)
    assert convert(path).markdown == "- first\n- second\n\n1. step one\n2. step two\n"


def test_ligatures_expanded(tmp_path):
    # PyMuPDF 的 HTML 排版會產生 ﬀ／ﬁ 連字，base-14 字型則無法
    path = tmp_path / "lig.pdf"
    story = pymupdf.Story(html="<p>different file office</p>", user_css="* {font-family: serif;}")
    writer = pymupdf.DocumentWriter(str(path))
    dev = writer.begin_page(pymupdf.paper_rect("a4"))
    story.place(pymupdf.Rect(50, 50, 500, 800))
    story.draw(dev)
    writer.end_page()
    writer.close()
    assert "different file office" in convert(path).markdown


def test_reading_order(sample_pdf):
    md = convert(sample_pdf).markdown
    order = ["# Sodium Disorders", "## Overview", "- Check", "### Key Thresholds", "| Parameter", "## Formula"]
    positions = [md.index(s) for s in order]
    assert positions == sorted(positions)


def test_images_extracted(sample_pdf, tmp_path):
    img_dir = tmp_path / "out" / "sample_images"
    result = convert(sample_pdf, image_dir=img_dir, image_link_base=tmp_path / "out")
    assert len(result.images) == 1 and result.images[0].exists()
    assert "![第 1 頁圖 1](sample_images/sample_p1_1.png)" in result.markdown
    # 圖片位於表格之後
    assert result.markdown.index("| U_osm") < result.markdown.index("![第 1 頁圖 1]")


def test_no_images_without_image_dir(sample_pdf):
    assert "![" not in convert(sample_pdf).markdown


def test_page_selection_and_breaks(sample_pdf):
    md = convert(sample_pdf, pages="2", page_breaks=True).markdown
    assert md.startswith("<!-- page 2 -->")
    assert "Sodium Disorders" not in md
    assert "Formula" in md


def test_bytes_input(sample_pdf):
    assert "# Sodium Disorders" in convert(sample_pdf.read_bytes()).markdown


def test_scanned_page_warning(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    page.insert_image(page.rect, pixmap=pix)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    result = convert(path)
    assert any("OCR" in w for w in result.warnings)


def test_encrypted_pdf(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 80), "Secret note", fontname="helv", fontsize=11)
    path = tmp_path / "locked.pdf"
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    with pytest.raises(PermissionError):
        convert(path)
    assert "Secret note" in convert(path, password="pw").markdown


def test_markdown_special_chars_escaped(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 80), "# not a heading with *stars* and file_name", fontname="helv", fontsize=11)
    path = tmp_path / "esc.pdf"
    doc.save(path)
    md = convert(path).markdown
    assert md.strip() == r"\# not a heading with \*stars\* and file\_name"


@pytest.mark.parametrize(
    "prev,nxt,expected",
    [
        ("hello", "world", "hello world"),
        ("electro-", "lyte", "electrolyte"),
        ("follow-", "Up", "follow- Up"),
        ("低血", "鈉", "低血鈉"),
        ("鈉離子", "Na", "鈉離子Na"),
        ("", "start", "start"),
    ],
)
def test_join_lines(prev, nxt, expected):
    assert join_lines(prev, nxt) == expected


def test_parse_page_spec():
    assert parse_page_spec(None, 3) == [0, 1, 2]
    assert parse_page_spec("1-2,5", 10) == [0, 1, 4]
    assert parse_page_spec("3-", 5) == [2, 3, 4]
    assert parse_page_spec("2-9", 3) == [1, 2]
    with pytest.raises(ValueError):
        parse_page_spec("0", 3)


# --------------------------------------------------------------------------
# 命令列
# --------------------------------------------------------------------------
def test_cli_default_output(sample_pdf):
    assert main([str(sample_pdf)]) == 0
    out = sample_pdf.with_suffix(".md")
    assert "# Sodium Disorders" in out.read_text(encoding="utf-8")


def test_cli_stdout(sample_pdf, capsys):
    assert main([str(sample_pdf), "-o", "-", "-p", "1"]) == 0
    assert "# Sodium Disorders" in capsys.readouterr().out


def test_cli_multiple_into_dir_with_images(tmp_path):
    a = build_sample_pdf(tmp_path / "a.pdf")
    b = build_sample_pdf(tmp_path / "b.pdf", with_image=False)
    out = tmp_path / "md"
    assert main([str(a), str(b), "-o", str(out), "--images"]) == 0
    assert (out / "a.md").exists() and (out / "b.md").exists()
    assert (out / "a_images" / "a_p1_1.png").exists()
    assert "](a_images/a_p1_1.png)" in (out / "a.md").read_text(encoding="utf-8")


def test_cli_missing_file(tmp_path, capsys):
    assert main([str(tmp_path / "nope.pdf")]) == 1
    assert "找不到檔案" in capsys.readouterr().err

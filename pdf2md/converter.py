"""PDF → Markdown 轉換核心。

以 PyMuPDF 讀出每一行文字的字級、字型與座標，再用版面推論還原 Markdown 結構：

- 標題：字級明顯大於內文者依大小分為 `#`～`####`；整段粗體的短行視為最低一級標題。
- 段落：同一區塊內的換行接回同一段（中日韓文字之間不補空格、英文斷字連字號會接回）；
  跨頁未結束的句子也會接回。
- 清單：`•`、`●`、`-` 等符號開頭的行轉為 `- `，依縮排推算巢狀層級；數字編號原樣保留。
- 表格：有框線的表格轉為 Markdown 表格，表格內文字不再重複輸出。
- 程式碼：整行皆為等寬字型者包成 ``` 區塊。
- 頁首頁尾：出現在多數頁面上下邊界的相同文字（忽略數字差異）與單獨頁碼會被移除。
- 圖片：指定 `image_dir` 時會另存並以相對路徑插入連結。

掃描檔（整頁只有圖片、沒有文字層）無法擷取文字，會在 warnings 中提示需要先做 OCR。
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union

import pymupdf

# PyMuPDF 會把「建議安裝 pymupdf_layout」印到標準輸出，會混進 `-o -` 的 Markdown
if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()

# --------------------------------------------------------------------------
# 規則常數
# --------------------------------------------------------------------------
CJK_RE = re.compile(r"[⺀-⿿　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")
BULLET_CHARS = "•●○◦▪▫■□◆◇‣⁃∙·・\\-–*"
BULLET_RE = re.compile(rf"^\s*[{BULLET_CHARS}]\s+")
BULLET_ONLY_RE = re.compile(rf"^\s*[{BULLET_CHARS}]\s*$")
ORDERED_RE = re.compile(r"^\s*(\d{1,3}[.)]|\(\d{1,3}\)|（\d{1,3}）)\s+")
SENTENCE_END_RE = re.compile(r"[.!?。！？：:;；」』）)\]\"”]$")
PAGE_NUMBER_RE = re.compile(
    r"^(?:[-–—]?\s*\d{1,4}\s*[-–—]?|第\s*\d+\s*頁(?:\s*[/／,，]?\s*共\s*\d+\s*頁)?|page\s*\d+(?:\s*(?:of|/)\s*\d+)?|\d+\s*/\s*\d+)$",
    re.IGNORECASE,
)
BOLD_FONT_RE = re.compile(r"bold|black|heavy|semibold|demi", re.IGNORECASE)
ITALIC_FONT_RE = re.compile(r"italic|oblique", re.IGNORECASE)
MONO_FONT_RE = re.compile(r"mono|courier|consol|menlo|code", re.IGNORECASE)

MARGIN_RATIO = 0.08          # 上下各 8% 視為頁首頁尾區
HEADING_SIZE_RATIO = 1.15    # 字級 ≥ 內文 × 1.15 才算標題
MAX_HEADING_LEVELS = 4
MAX_HEADING_CHARS = 200
MIN_IMAGE_PX = 32
INDENT_STEP_PT = 18
CAPTION_RE = re.compile(r"^\s*(?:table|tab\.|表)\s*[\dIVXivx]+", re.IGNORECASE)
FIGURE_RE = re.compile(r"^\s*(?:figure|fig\.|圖)\s*\d+", re.IGNORECASE)
COLUMN_GAP_PT = 20           # 無框線表格中，行首 x 座標相距超過此值視為不同欄
MAX_LAYOUT_CELL_CHARS = 400
FIGURE_DPI = 150
MIN_FIGURE_DRAWINGS = 3
FIGURE_GAP_PT = 30           # 圖與圖說、圖內各繪圖元件之間允許的最大垂直間距  # 有框線「表格」的儲存格超過此長度，多半是排版用的外框（如摘要欄）


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------
@dataclass
class Span:
    text: str
    bold: bool
    italic: bool
    mono: bool
    font: str = ""


@dataclass
class Line:
    spans: List[Span]
    size: float
    bbox: tuple
    page: int
    block: int

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def all_bold(self) -> bool:
        visible = [s for s in self.spans if s.text.strip()]
        return bool(visible) and all(s.bold for s in visible)

    @property
    def all_mono(self) -> bool:
        visible = [s for s in self.spans if s.text.strip()]
        return bool(visible) and all(s.mono for s in visible)

    @property
    def font(self) -> str:
        """以字元數加權的主要字型。"""
        weights: Counter = Counter()
        for s in self.spans:
            weights[s.font] += len(s.text.strip())
        return weights.most_common(1)[0][0] if weights else ""

    @property
    def pure(self) -> bool:
        """整行只用一種字型。"""
        return len({s.font for s in self.spans if s.text.strip()}) <= 1

    @property
    def key(self) -> tuple:
        return (self.font, self.size)


@dataclass
class Item:
    """輸出單位：heading / para / bullet / ordered / code / table / image / pagebreak。"""
    kind: str
    text: str = ""
    level: int = 0
    size: float = 0.0
    page: int = 0
    x0: float = 0.0
    y: float = 0.0


@dataclass
class ConversionResult:
    markdown: str
    page_count: int
    warnings: List[str] = field(default_factory=list)
    images: List[Path] = field(default_factory=list)


# --------------------------------------------------------------------------
# 文字工具
# --------------------------------------------------------------------------
def _is_cjk(ch: str) -> bool:
    return bool(ch) and bool(CJK_RE.match(ch))


def join_lines(prev: str, nxt: str) -> str:
    """把換行接回同一段：英文補空格、中日韓不補、英文斷字連字號接回。"""
    prev = prev.rstrip()
    nxt = nxt.lstrip()
    if not prev:
        return nxt
    if not nxt:
        return prev
    if prev.endswith("-") and len(prev) >= 2 and prev[-2].isalpha() and nxt[0].islower():
        # 複合詞（evidence-to-）的連字號要保留，一般斷字（electro-）才接回
        last_word = prev.split()[-1]
        if "-" in last_word[:-1]:
            return prev + nxt
        return prev[:-1] + nxt
    sep = "" if _is_cjk(prev[-1]) or _is_cjk(nxt[0]) else " "
    # 粗體跨行時接成同一段粗體：「**a**」+「**b**」→「**a b**」
    if re.search(r"(?<![*\\])\*\*$", prev) and re.match(r"\*\*(?!\*)", nxt) and not prev.endswith("***"):
        return prev[:-2] + sep + nxt[2:]
    return prev + sep + nxt


def escape_md(text: str) -> str:
    return re.sub(r"([\\`*_])", r"\\\1", text)


def _escape_line_start(text: str) -> str:
    # 內文行首的 # 或 > 會被誤判為標題／引用
    return re.sub(r"^(\s*)([#>])", r"\1\\\2", text)


def _span_style(span: dict) -> Span:
    flags = span.get("flags", 0)
    font = span.get("font", "")
    return Span(
        text=span.get("text", ""),
        bold=bool(flags & 16) or bool(BOLD_FONT_RE.search(font)),
        italic=bool(flags & 2) or bool(ITALIC_FONT_RE.search(font)),
        mono=bool(flags & 8) or bool(MONO_FONT_RE.search(font)),
        font=re.sub(r"^[A-Z]{6}\+", "", font),  # 去掉子集字型前綴 ABCDEF+
    )


def render_spans(spans: Sequence[Span], *, plain: bool = False) -> str:
    """將 spans 轉為行內 Markdown；相鄰同樣式的 span 先合併，避免 `**a****b**`。"""
    merged: List[Span] = []
    for s in spans:
        if not s.text:
            continue
        if merged and (merged[-1].bold, merged[-1].italic, merged[-1].mono) == (s.bold, s.italic, s.mono):
            merged[-1] = Span(merged[-1].text + s.text, s.bold, s.italic, s.mono)
        else:
            merged.append(Span(s.text, s.bold, s.italic, s.mono))

    out = []
    for s in merged:
        if plain:
            out.append(escape_md(s.text))
            continue
        core = s.text.strip()
        if not core:
            out.append(s.text)
            continue
        lead = s.text[: len(s.text) - len(s.text.lstrip())]
        trail = s.text[len(s.text.rstrip()):]
        if s.mono:
            tick = "``" if "`" in core else "`"
            core = f"{tick}{core}{tick}"
        else:
            core = escape_md(core)
            if s.bold and s.italic:
                core = f"***{core}***"
            elif s.bold:
                core = f"**{core}**"
            elif s.italic:
                core = f"*{core}*"
        out.append(f"{lead}{core}{trail}")
    return "".join(out)


def parse_page_spec(spec: Optional[str], page_count: int) -> List[int]:
    """'1-3,5' → [0, 1, 2, 4]（0 起算）。超出範圍的頁碼會被略過。"""
    if not spec:
        return list(range(page_count))
    pages: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = int(a) if a.strip() else 1
            end = int(b) if b.strip() else page_count
        else:
            start = end = int(part)
        if start < 1 or end < start:
            raise ValueError(f"頁碼範圍不正確：{part}")
        for p in range(start, min(end, page_count) + 1):
            if p - 1 not in pages:
                pages.append(p - 1)
    return pages


# --------------------------------------------------------------------------
# 擷取
# --------------------------------------------------------------------------
def _inside(bbox: tuple, rects: Iterable[tuple]) -> bool:
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return any(r[0] - 1 <= cx <= r[2] + 1 and r[1] - 1 <= cy <= r[3] + 1 for r in rects)


def _cell_text(cell: Optional[str]) -> str:
    if not cell:
        return ""
    text = ""
    for part in cell.splitlines():
        text = join_lines(text, part)
    return text.replace("|", "\\|").strip()


def table_to_markdown(rows: List[List[Optional[str]]]) -> str:
    rows = [[_cell_text(c) for c in row] for row in rows]
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    # 去掉整欄皆空的欄位（合併儲存格常造成）
    keep = [i for i in range(width) if any(r[i] for r in rows)]
    rows = [[r[i] for i in keep] for r in rows]
    header, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _extract_page(page: "pymupdf.Page", page_no: int, *, detect_tables: bool):
    """回傳 (lines, tables, images)，tables/images 為 (y0, x0, payload)。"""
    tables = []
    table_rects = []
    if detect_tables:
        try:
            found = page.find_tables()
        except Exception:  # find_tables 對少數損毀頁面會丟例外，退回純文字
            found = None
        for tab in (found.tables if found else []):
            # 以儲存格範圍取字：tab.extract() 會把底線等低於基線的字元排錯位置
            rows = [
                [page.get_textbox(cell) if cell else None for cell in row.cells]
                for row in tab.rows
            ]
            if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
                continue
            if not any(any(c for c in r) for r in rows):
                continue
            if max(len(c or "") for r in rows for c in r) > MAX_LAYOUT_CELL_CHARS:
                continue
            table_rects.append(tuple(tab.bbox))
            tables.append((tab.bbox[1], tab.bbox[0], table_to_markdown(rows)))

    data = page.get_text(
        "dict",
        # 不保留連字（ﬁ、ﬀ…），讓英文單字可被搜尋
        flags=(pymupdf.TEXTFLAGS_DICT | pymupdf.TEXT_PRESERVE_IMAGES) & ~pymupdf.TEXT_PRESERVE_LIGATURES,
        sort=True,
    )
    lines: List[Line] = []
    images = []
    W, H = page.rect.width, page.rect.height
    dirs = Counter(
        (round(ln["dir"][0]), round(ln["dir"][1]))
        for b in data["blocks"] for ln in b.get("lines", [])
    )
    main_dir = dirs.most_common(1)[0][0] if dirs else (1, 0)
    for b_idx, block in enumerate(data["blocks"]):
        if block.get("type") == 1:
            if block.get("width", 0) >= MIN_IMAGE_PX and block.get("height", 0) >= MIN_IMAGE_PX:
                images.append((block["bbox"][1], block["bbox"][0], block))
            continue
        for ln in block.get("lines", []):
            if _inside(ln["bbox"], table_rects):
                continue
            spans = [_span_style(s) for s in ln["spans"]]
            text = "".join(s.text for s in spans)
            if not text.strip():
                continue
            # 以字元數加權取該行代表字級
            weights = Counter()
            for s in ln["spans"]:
                weights[round(s["size"] * 2) / 2] += len(s["text"].strip())
            size = weights.most_common(1)[0][0] if weights else 0.0
            bbox = tuple(ln["bbox"])
            if main_dir != (1, 0) and (round(ln["dir"][0]), round(ln["dir"][1])) == main_dir:
                bbox = _derotate(bbox, main_dir, W, H)
            lines.append(Line(spans=spans, size=size, bbox=bbox, page=page_no, block=b_idx))
    if main_dir != (1, 0):
        # 橫印（旋轉 90°）的頁面：換到閱讀方向的座標後，依區塊重新排序
        first = {}
        for ln in lines:
            first.setdefault(ln.block, (ln.bbox[1], ln.bbox[0]))
        lines.sort(key=lambda ln: first[ln.block])
    return lines, tables, images


def _derotate(bbox: tuple, direction: tuple, W: float, H: float) -> tuple:
    """把旋轉文字的 bbox 轉成「文字由左至右、由上至下」的閱讀座標。"""
    x0, y0, x1, y1 = bbox
    if direction == (0, -1):   # 由下往上寫
        return (H - y1, x0, H - y0, x1)
    if direction == (0, 1):    # 由上往下寫
        return (y0, W - x1, y1, W - x0)
    if direction == (-1, 0):   # 上下顛倒
        return (W - x1, H - y1, W - x0, H - y0)
    return bbox


def _repeated_margin_keys(page_lines: dict, page_heights: dict) -> set:
    """找出在多數頁面上下邊界重複出現的文字（數字視為相同）。"""
    if len(page_lines) < 2:
        return set()
    counter: Counter = Counter()
    for p, lines in page_lines.items():
        h = page_heights[p]
        keys = {
            re.sub(r"\d+", "#", ln.text.strip())
            for ln in lines
            if ln.bbox[3] <= h * MARGIN_RATIO or ln.bbox[1] >= h * (1 - MARGIN_RATIO)
        }
        counter.update(keys)
    threshold = max(2, (len(page_lines) + 1) // 2)
    return {k for k, n in counter.items() if n >= threshold}


def _in_margin(line: Line, height: float) -> bool:
    return line.bbox[3] <= height * MARGIN_RATIO or line.bbox[1] >= height * (1 - MARGIN_RATIO)


# --------------------------------------------------------------------------
# 無框線表格
# --------------------------------------------------------------------------
def _caption_tables(lines: List[Line], body_size: float) -> tuple:
    """找出「TABLE n」標題下方、字級小於內文的無框線表格，回傳 (剩餘行, [(y, x0, markdown)])。

    期刊表格常只靠對齊排版、沒有框線。作法：
    - 範圍：標題之後連續、字級小於內文的行；遇到內文字級、下一個圖表標題、
      大段空白，或橫跨多欄的行（通常是表格註腳）即結束。
    - 欄：行首 x 座標分群。
    - 列：第一欄裡的「儲存格起始行」開新列；懸掛縮排、同字型且緊接在下的行是同一格的續行。
      其他欄的行依 y 座標歸入所在列，同格內多個起始行以 <br> 分隔。
    """
    ordered = sorted(lines, key=lambda ln: (ln.bbox[1], ln.bbox[0]))
    taken: set = set()
    tables = []
    for i, cap in enumerate(ordered):
        if id(cap) in taken or not CAPTION_RE.match(cap.text):
            continue
        region: List[Line] = []
        bottom = cap.bbox[3]
        for ln in ordered[i + 1:]:
            if ln.bbox[1] < cap.bbox[3] - 1:
                continue
            if ln.size >= body_size - 0.25 or CAPTION_RE.match(ln.text) or FIGURE_RE.match(ln.text):
                break
            gap = ln.bbox[1] - bottom
            if gap > ln.size * (5 if not region else 2.5):
                break
            region.append(ln)
            bottom = max(bottom, ln.bbox[3])
        table = _layout_table(region)
        if table is None:
            continue
        md, used = table
        taken.update(id(ln) for ln in used)
        tables.append((min(ln.bbox[1] for ln in used), min(ln.bbox[0] for ln in used), md))
    remaining = [ln for ln in lines if id(ln) not in taken]
    return remaining, tables


def _layout_table(region: List[Line]) -> Optional[tuple]:
    if len(region) < 2:
        return None
    xs = sorted(ln.bbox[0] for ln in region)
    starts = [xs[0]]
    for a, b in zip(xs, xs[1:]):
        if b - a > COLUMN_GAP_PT:
            starts.append(b)
    if len(starts) < 2:
        return None

    def col_of(ln: Line) -> int:
        return max(i for i, x in enumerate(starts) if x <= ln.bbox[0] + 2)

    # 橫跨到下一欄的行（表格註腳）之後就不屬於表格
    used: List[Line] = []
    for ln in region:
        c = col_of(ln)
        if c + 1 < len(starts) and ln.bbox[2] > starts[c + 1] + 2:
            break
        used.append(ln)
    if len(used) < 2 or len({col_of(ln) for ln in used}) < 2:
        return None

    # 每欄標記儲存格起始行
    cells: dict = {}  # col -> [[start_line, [lines...]], ...]
    for c in range(len(starts)):
        col_lines = [ln for ln in used if col_of(ln) == c]
        indent_levels = {round(ln.bbox[0]) for ln in col_lines}
        hanging = len(indent_levels) >= 2
        current = None
        out = []
        for ln in col_lines:
            close = current is not None and ln.bbox[1] - current[1][-1].bbox[3] < 0.8 * ln.size
            same_font = current is not None and ln.font == current[0].font
            if current is not None and close and same_font and (
                ln.bbox[0] > current[0].bbox[0] + 2 if hanging else c != 0
            ):
                current[1].append(ln)
            else:
                current = [ln, [ln]]
                out.append(current)
        cells[c] = out

    row_starts = [cell[0].bbox[1] for cell in cells.get(0, [])]
    if not row_starts:
        return None

    def row_of(y: float) -> int:
        idx = 0
        for i, ry in enumerate(row_starts):
            if ry <= y + 2:
                idx = i
        return idx

    grid = [["" for _ in starts] for _ in row_starts]
    for c, col_cells in cells.items():
        for start, cell_lines in col_cells:
            text = ""
            for ln in cell_lines:
                text = join_lines(text, render_spans(ln.spans))
            text = text.replace("|", "\\|")
            r = row_of(start.bbox[1])
            grid[r][c] = f"{grid[r][c]}<br>{text}" if grid[r][c] else text
    if len(grid) < 2:
        return None

    # 第一列的字型與其餘列不同（例如粗體欄名）才當表頭，否則用空白表頭
    def fonts_of(r: int) -> list:
        return [next((st.font for st, _ in cells[c] if row_of(st.bbox[1]) == r), None) for c in cells]

    first = fonts_of(0)
    rest = [fonts_of(r) for r in range(1, len(grid))]
    is_header = all(first) and all(
        all(f != other[c] for other in rest if other[c]) for c, f in enumerate(first)
    )
    header, body = (grid[0], grid[1:]) if is_header else ([""] * len(starts), grid)
    md = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    md += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(md), used


def _vector_figures(
    page: "pymupdf.Page", lines: Sequence[Line], body_size: float, image_boxes: Sequence[tuple] = ()
) -> List["pymupdf.Rect"]:
    """在「FIGURE n」標題上方，由緊鄰的向量繪圖往上延伸出圖的範圍。"""
    if page.rotation:
        return []
    drawings = [d["rect"] for d in page.get_drawings()]
    figures = []
    for cap in lines:
        if not FIGURE_RE.match(cap.text):
            continue
        top, bottom = cap.bbox[1], cap.bbox[1]
        members = []
        for r in sorted((r for r in drawings if r.y1 <= cap.bbox[1] + 1), key=lambda r: -r.y1):
            if r.y1 < top - FIGURE_GAP_PT:
                break
            members.append(r)
            top = min(top, r.y0)
        if len(members) < MIN_FIGURE_DRAWINGS:
            continue
        rect = pymupdf.Rect(members[0])
        for r in members[1:]:
            rect |= r
        rect.y1 = min(rect.y1, bottom)
        # 圖上方或圖內字級較小的標籤、緊鄰的點陣圖也算圖的一部分
        parts = [pymupdf.Rect(ln.bbox) for ln in lines if ln is not cap and ln.size < body_size - 0.25]
        parts += [pymupdf.Rect(b) for b in image_boxes]
        grew = True
        while grew:
            grew = False
            for b in parts:
                if b in rect:
                    continue
                if b.y1 >= rect.y0 - 12 and b.y0 <= rect.y1 and b.x0 >= rect.x0 - 5 and b.x1 <= rect.x1 + 5:
                    rect |= b
                    grew = True
        if rect.height >= 40 and rect.width >= 40:
            figures.append(rect + (-4, -4, 4, 4))
    return figures


# --------------------------------------------------------------------------
# 結構推論
# --------------------------------------------------------------------------
def _body_size(lines: Sequence[Line]) -> float:
    sizes: Counter = Counter()
    for ln in lines:
        sizes[ln.size] += len(ln.text.strip())
    return sizes.most_common(1)[0][0] if sizes else 0.0


def _heading_levels(lines: Sequence[Line]) -> tuple:
    """回傳 (內文字級, {字級: 標題層級}, {(字型, 字級): 標題層級})。

    字級明顯大於內文者依大小排序為前幾級；再來是與內文同級、但用獨特字型的短行
    （期刊常以不同字型而非粗體旗標標示小節標題，字型名稱也常被混淆成 AdvTT3e3c8cd7 之類），
    依在文件中首次出現的順序往下排。
    """
    if not lines:
        return 0.0, {}, {}
    body = _body_size(lines)
    big = sorted({ln.size for ln in lines if ln.size >= body * HEADING_SIZE_RATIO and ln.size - body >= 1}, reverse=True)
    size_levels = {s: min(i + 1, MAX_HEADING_LEVELS) for i, s in enumerate(big)}

    fonts: Counter = Counter()
    for ln in lines:
        if ln.size == body:
            fonts[ln.font] += len(ln.text.strip())
    body_font = fonts.most_common(1)[0][0] if fonts else ""

    # 統計每種 (字型, 字級) 的「連續行」：同一區塊內相鄰且同字型字級的行視為一段
    stats: dict = {}
    prev: Optional[Line] = None
    run: Optional[dict] = None
    for idx, ln in enumerate(lines):
        st = stats.setdefault(ln.key, {"lines": 0, "pure": 0, "mono": 0, "runs": [], "first": idx})
        st["lines"] += 1
        st["pure"] += ln.pure
        st["mono"] += ln.all_mono
        same_block = prev is not None and (prev.page, prev.block) == (ln.page, ln.block)
        if same_block and prev.key == ln.key:
            run["text"] = join_lines(run["text"], ln.text)
        else:
            # 位置合理的標題：在區塊開頭，或緊接在另一種非內文字型之後
            placed = not same_block or prev.font != body_font
            run = {"text": ln.text.strip(), "placed": placed}
            st["runs"].append(run)
        prev = ln

    candidates = []
    for key, st in stats.items():
        font, size = key
        if font == body_font or size in size_levels or size < body - 0.25 or st["mono"]:
            continue
        runs = st["runs"]
        lengths = sorted(_display_len(r["text"]) for r in runs)
        if (
            st["pure"] >= 0.8 * st["lines"]
            and lengths[len(lengths) // 2] <= 150
            and sum(n <= 200 for n in lengths) >= 0.8 * len(runs)
            and sum(bool(re.search(r"[.。!！]$", r["text"])) for r in runs) <= 0.3 * len(runs)
            and sum(r["placed"] for r in runs) >= 0.8 * len(runs)
        ):
            candidates.append((st["first"], key))
    base = max(size_levels.values(), default=0)
    font_levels = {key: min(base + i + 1, 6) for i, (_, key) in enumerate(sorted(candidates))}
    return body, size_levels, font_levels


def _display_len(text: str) -> int:
    """中日韓字元算兩個寬度，讓長度門檻對中英文一致。"""
    return sum(2 if _is_cjk(ch) else 1 for ch in text)


def _lines_to_items(lines: Sequence[Line], body: float, levels: dict, font_levels: Optional[dict] = None) -> List[Item]:
    font_levels = font_levels or {}
    bold_level = min(max(list(levels.values()) + list(font_levels.values()), default=0) + 1, 6)
    items: List[Item] = []
    pending_bullet: Optional[Line] = None  # 符號與文字分開排版時，符號會單獨成一行甚至一個區塊

    def boundary(a: Line, b: Line) -> bool:
        if a.page != b.page:
            return True
        if a.block != b.block:
            # 有些 PDF 每行自成一個區塊：緊接在下、左緣對齊、同字型字級的行仍屬同一段
            gap = b.bbox[1] - a.bbox[3]
            return not (a.key == b.key and abs(a.bbox[0] - b.bbox[0]) <= 2 and -0.3 * a.size <= gap < 0.5 * a.size)
        # 與內文同一區塊的標題（Introduction 後面直接接段落）要切開
        if (a.key in font_levels or b.key in font_levels) and a.key != b.key:
            return True
        return (a.size in levels or b.size in levels) and a.size != b.size

    groups: List[List[Line]] = []
    for ln in lines:
        if groups and not boundary(groups[-1][-1], ln):
            groups[-1].append(ln)
        else:
            groups.append([ln])

    for group in groups:
        first = group[0]
        plain = "".join(ln.text for ln in group).strip()

        # 程式碼：整組皆等寬字型
        if all(ln.all_mono for ln in group):
            items.append(Item("code", "\n".join(ln.text.rstrip() for ln in group), page=first.page, x0=first.bbox[0], y=first.bbox[1]))
            continue

        # 標題：大字級
        if first.size in levels and all(ln.size == first.size for ln in group) and len(plain) <= MAX_HEADING_CHARS:
            text = ""
            for ln in group:
                text = join_lines(text, render_spans(ln.spans, plain=True))
            items.append(Item("heading", text, level=levels[first.size], size=first.size, page=first.page, y=first.bbox[1]))
            continue

        # 標題：與內文同級、但用獨特字型
        if first.key in font_levels and all(ln.key == first.key for ln in group) and _display_len(plain) <= MAX_HEADING_CHARS:
            text = ""
            for ln in group:
                text = join_lines(text, render_spans(ln.spans, plain=True))
            items.append(Item("heading", text, level=font_levels[first.key], size=first.size, page=first.page, y=first.bbox[1]))
            continue

        # 標題：內文字級、整段粗體的短行（不以句號結尾、也不是清單項）
        if (
            len(group) <= 2
            and all(ln.all_bold for ln in group)
            and len(plain) <= 60
            and not re.search(r"[.。]$", plain)
            and not BULLET_RE.match(plain)
            and not BULLET_ONLY_RE.match(plain)
            and abs(first.size - body) < 1
        ):
            text = ""
            for ln in group:
                text = join_lines(text, render_spans(ln.spans, plain=True))
            items.append(Item("heading", text, level=bold_level, size=first.size, page=first.page, y=first.bbox[1]))
            continue

        # 一般段落與清單：清單符號或編號開頭的行另起一項，其餘行接到目前這項
        current: Optional[Item] = None
        for ln in group:
            raw = ln.text
            rendered = render_spans(ln.spans)
            if BULLET_ONLY_RE.match(raw):
                pending_bullet = ln
                continue
            if pending_bullet is not None:
                current = Item("bullet", rendered.strip(), size=ln.size, page=ln.page,
                               x0=pending_bullet.bbox[0], y=pending_bullet.bbox[1])
                items.append(current)
                pending_bullet = None
            elif BULLET_RE.match(raw):
                marker_len = len(BULLET_RE.match(raw).group(0))
                body_text = render_spans(_drop_prefix(ln.spans, marker_len))
                current = Item("bullet", body_text.strip(), size=ln.size, page=ln.page, x0=ln.bbox[0], y=ln.bbox[1])
                items.append(current)
            elif ORDERED_RE.match(raw):
                current = Item("ordered", rendered.strip(), size=ln.size, page=ln.page, x0=ln.bbox[0], y=ln.bbox[1])
                items.append(current)
            elif current is None:
                current = Item("para", rendered.strip(), size=ln.size, page=ln.page, x0=ln.bbox[0], y=ln.bbox[1])
                items.append(current)
            else:
                current.text = join_lines(current.text, rendered)
    return items


def _drop_prefix(spans: Sequence[Span], n: int) -> List[Span]:
    out = []
    for s in spans:
        if n <= 0:
            out.append(s)
        elif len(s.text) <= n:
            n -= len(s.text)
        else:
            out.append(Span(s.text[n:], s.bold, s.italic, s.mono))
            n = 0
    return out


def _merge_across_breaks(items: List[Item]) -> List[Item]:
    """段落在區塊或頁面邊界被切開、且前段未以句讀結尾時接回。"""
    merged: List[Item] = []
    for it in items:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.kind == "para"
            and it.kind == "para"
            and abs(prev.size - it.size) < 0.5
            and prev.page != it.page
            and not SENTENCE_END_RE.search(prev.text)
        ):
            prev.text = join_lines(prev.text, it.text)
            continue
        merged.append(it)
    return merged


def render_items(items: Sequence[Item]) -> str:
    out: List[str] = []
    i = 0
    while i < len(items):
        it = items[i]
        if it.kind in ("bullet", "ordered"):
            run = [it]
            i += 1
            while i < len(items) and items[i].kind in ("bullet", "ordered"):
                # 最外層的項目符號與數字編號交替時，拆成兩個清單
                if items[i].kind != it.kind and items[i].x0 <= it.x0 + 1:
                    break
                run.append(items[i])
                i += 1
            base = min(r.x0 for r in run)
            lines = []
            for r in run:
                indent = "  " * min(3, int((r.x0 - base) / INDENT_STEP_PT))
                prefix = "- " if r.kind == "bullet" else ""
                lines.append(f"{indent}{prefix}{r.text}")
            out.append("\n".join(lines))
            continue
        if it.kind == "heading":
            out.append(f"{'#' * it.level} {it.text}")
        elif it.kind == "para":
            out.append(_escape_line_start(it.text))
        elif it.kind == "code":
            fence = "````" if "```" in it.text else "```"
            out.append(f"{fence}\n{it.text}\n{fence}")
        elif it.kind in ("table", "image", "pagebreak"):
            out.append(it.text)
        i += 1
    return "\n\n".join(out).strip() + "\n"


# --------------------------------------------------------------------------
# 對外 API
# --------------------------------------------------------------------------
def convert(
    source: Union[str, os.PathLike, bytes],
    *,
    pages: Optional[str] = None,
    image_dir: Optional[Union[str, os.PathLike]] = None,
    image_link_base: Optional[Union[str, os.PathLike]] = None,
    page_breaks: bool = False,
    keep_headers: bool = False,
    detect_tables: bool = True,
    password: Optional[str] = None,
) -> ConversionResult:
    """把 PDF 轉為 Markdown。

    source：PDF 路徑或位元組內容。
    pages：要轉換的頁碼，如 "1-3,5"（1 起算）；None 表示全部。
    image_dir：指定時把圖片另存於此資料夾並在 Markdown 中插入連結。
    image_link_base：圖片連結相對於哪個資料夾（通常是輸出 .md 所在資料夾）；預設為 image_dir 的上一層。
    page_breaks：在每頁開頭插入 `<!-- page N -->` 註解。
    keep_headers：保留頁首頁尾與頁碼。
    detect_tables：偵測有框線的表格並轉為 Markdown 表格。
    password：加密 PDF 的密碼。
    """
    if isinstance(source, (bytes, bytearray)):
        doc = pymupdf.open(stream=bytes(source), filetype="pdf")
        stem = "document"
    else:
        doc = pymupdf.open(str(source))
        stem = Path(source).stem
    warnings: List[str] = []
    saved: List[Path] = []

    with doc:
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise PermissionError("這份 PDF 有密碼保護，請以 password 參數提供密碼。")

        page_nos = parse_page_spec(pages, doc.page_count)
        page_lines: dict = {}
        page_heights: dict = {}
        page_tables: dict = {}
        page_images: dict = {}
        scanned = []
        for p in page_nos:
            page = doc[p]
            lines, tables, images = _extract_page(page, p, detect_tables=detect_tables)
            page_lines[p] = lines
            page_heights[p] = page.rect.height
            page_tables[p] = tables
            page_images[p] = images
            if not lines and not tables and page.get_images():
                scanned.append(p + 1)

        if not keep_headers:
            repeated = _repeated_margin_keys(page_lines, page_heights)
            for p, lines in page_lines.items():
                page_lines[p] = [
                    ln for ln in lines
                    if not (
                        _in_margin(ln, page_heights[p])
                        and (
                            re.sub(r"\d+", "#", ln.text.strip()) in repeated
                            or PAGE_NUMBER_RE.match(ln.text.strip())
                        )
                    )
                ]

        if detect_tables:
            body_size = _body_size([ln for p in page_nos for ln in page_lines[p]])
            for p in page_nos:
                page_lines[p], found = _caption_tables(page_lines[p], body_size)
                page_tables[p].extend(found)

        all_lines = [ln for p in page_nos for ln in page_lines[p]]
        body, levels, font_levels = _heading_levels(all_lines)

        if image_dir is not None:
            image_dir = Path(image_dir)
            link_base = Path(image_link_base) if image_link_base is not None else image_dir.parent

        items: List[Item] = []
        for p in page_nos:
            if page_breaks:
                items.append(Item("pagebreak", f"<!-- page {p + 1} -->", page=p))
            extras = [Item("table", md, page=p, y=y) for y, _, md in page_tables[p]]
            if image_dir is not None:
                # 向量繪製的圖（流程圖、統計圖）存成圖片，並移除圖內被打散的文字標籤
                figures = _vector_figures(doc[p], page_lines[p], body, [im[2]["bbox"] for im in page_images[p]])
                for n, rect in enumerate(figures, start=1):
                    image_dir.mkdir(parents=True, exist_ok=True)
                    path = image_dir / f"{stem}_p{p + 1}_fig{n}.png"
                    doc[p].get_pixmap(clip=rect, dpi=FIGURE_DPI).save(path)
                    saved.append(path)
                    rel = Path(os.path.relpath(path, link_base)).as_posix()
                    extras.append(Item("image", f"![第 {p + 1} 頁圖表 {n}]({rel})", page=p, y=rect.y0))
                fig_boxes = [tuple(r) for r in figures]
                page_lines[p] = [ln for ln in page_lines[p] if not _inside(ln.bbox, fig_boxes)]
                page_images[p] = [im for im in page_images[p] if not _inside(im[2]["bbox"], fig_boxes)]
            text_items = _lines_to_items(page_lines[p], body, levels, font_levels)
            if image_dir is not None:
                for n, (y, _, block) in enumerate(page_images[p], start=1):
                    image_dir.mkdir(parents=True, exist_ok=True)
                    path = image_dir / f"{stem}_p{p + 1}_{n}.{block.get('ext', 'png')}"
                    path.write_bytes(block["image"])
                    saved.append(path)
                    rel = Path(os.path.relpath(path, link_base)).as_posix()
                    extras.append(Item("image", f"![第 {p + 1} 頁圖 {n}]({rel})", page=p, y=y))
            items.extend(_insert_by_position(text_items, extras))

        items = _merge_across_breaks(_compact_heading_levels(items))
        markdown = render_items(items)

        if scanned:
            warnings.append(
                f"第 {_format_pages(scanned)} 頁沒有文字層（可能是掃描影像），無法擷取文字，需先做 OCR。"
            )
        if not markdown.strip():
            warnings.append("沒有擷取到任何內容。")

        return ConversionResult(markdown=markdown, page_count=doc.page_count, warnings=warnings, images=saved)


def _compact_heading_levels(items: List[Item]) -> List[Item]:
    """把實際用到的標題層級重新編號為 1、2、3…，避免跳級（例如只有 # 與 ####）。"""
    used = sorted({it.level for it in items if it.kind == "heading"})
    remap = {lvl: i + 1 for i, lvl in enumerate(used)}
    for it in items:
        if it.kind == "heading":
            it.level = remap[it.level]
    return items


def _insert_by_position(text_items: Sequence[Item], extras: Sequence[Item]) -> List[Item]:
    """表格與圖片插在第一個位於其下方的文字項之前，文字項本身的順序不變。"""
    out = list(text_items)
    for ex in sorted(extras, key=lambda e: e.y):
        idx = next((i for i, it in enumerate(out) if it.kind not in ("table", "image") and it.y > ex.y), len(out))
        out.insert(idx, ex)
    return out


def _format_pages(pages: Sequence[int]) -> str:
    ranges = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = p
    ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    return "、".join(ranges)

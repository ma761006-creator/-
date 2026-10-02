"""命令列介面：python -m pdf2md 檔案.pdf [...]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .converter import convert


def _output_path(src: Path, output: str | None, multiple: bool) -> Path | None:
    """回傳輸出檔路徑；None 代表輸出到標準輸出。"""
    if output == "-":
        return None
    if output is None:
        return src.with_suffix(".md")
    out = Path(output)
    if multiple or out.is_dir() or output.endswith(("/", "\\")):
        return out / f"{src.stem}.md"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pdf2md",
        description="把 PDF 轉為 Markdown（標題、段落、清單、表格、程式碼、圖片）。",
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="一或多個 PDF 檔")
    parser.add_argument(
        "-o", "--output",
        help="輸出檔名；多個輸入時為輸出資料夾；'-' 印到標準輸出。預設與 PDF 同名同資料夾的 .md",
    )
    parser.add_argument("-p", "--pages", help="只轉換指定頁，例如 1-3,5")
    parser.add_argument("--images", action="store_true", help="另存圖片到 <檔名>_images/ 並在 Markdown 中插入連結")
    parser.add_argument("--page-breaks", action="store_true", help="在每頁開頭插入 <!-- page N --> 註解")
    parser.add_argument("--keep-headers", action="store_true", help="保留頁首、頁尾與頁碼")
    parser.add_argument("--no-tables", action="store_true", help="不偵測表格，全部當作文字")
    parser.add_argument("--password", help="加密 PDF 的密碼")
    args = parser.parse_args(argv)

    multiple = len(args.inputs) > 1
    if multiple and args.output == "-":
        parser.error("多個輸入檔時不能輸出到標準輸出")

    failed = 0
    for src in args.inputs:
        if not src.is_file():
            print(f"✗ 找不到檔案：{src}", file=sys.stderr)
            failed += 1
            continue
        dest = _output_path(src, args.output, multiple)
        image_dir = None
        if args.images:
            base = dest.parent if dest else Path.cwd()
            image_dir = base / f"{(dest or src).stem}_images"
        try:
            result = convert(
                src,
                pages=args.pages,
                image_dir=image_dir,
                image_link_base=dest.parent if dest else Path.cwd(),
                page_breaks=args.page_breaks,
                keep_headers=args.keep_headers,
                detect_tables=not args.no_tables,
                password=args.password,
            )
        except Exception as exc:  # 單一檔案失敗不影響其他檔案
            print(f"✗ {src}：{exc}", file=sys.stderr)
            failed += 1
            continue

        if dest is None:
            sys.stdout.write(result.markdown)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(result.markdown, encoding="utf-8")
            extra = f"，{len(result.images)} 張圖片" if result.images else ""
            print(f"✓ {src} → {dest}（{result.page_count} 頁{extra}）", file=sys.stderr)
        for w in result.warnings:
            print(f"  ⚠ {w}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

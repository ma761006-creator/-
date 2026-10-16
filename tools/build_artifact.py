#!/usr/bin/env python3
"""從 static/index.html 產出 Claude Artifact 版本的頁面。

Artifact 會在發佈時自行包上 <!doctype>/<html>/<head>/<body>，因此發佈用的檔案
只能是「head 內容 + body 內容」。本腳本依 index.html 裡的 ARTIFACT 標記切出這
兩段，讓 repo 內維持單一份原始碼。

用法：python tools/build_artifact.py [輸出路徑]
"""

import re
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "static" / "index.html"


def extract(html: str, name: str) -> str:
    match = re.search(
        rf"<!-- ARTIFACT:{name} -->(.*?)<!-- /ARTIFACT:{name} -->", html, re.S
    )
    if match is None:
        raise SystemExit(f"index.html 缺少 ARTIFACT:{name} 標記")
    return match.group(1).strip("\n")


def build(html: str) -> str:
    return extract(html, "HEAD") + "\n\n" + extract(html, "BODY") + "\n"


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifact-page.html")
    out.write_text(build(SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"{out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

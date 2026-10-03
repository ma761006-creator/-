/*
 * pdf2md 網頁版核心：在瀏覽器（或 Node）以 pdf.js 讀取 PDF，轉為 Markdown。
 *
 * 版面推論規則移植自 pdf2md/converter.py（Python 版），兩邊應保持一致：
 * 字級／獨特字型推得標題、段落與跨頁接回、清單、等寬程式碼、頁首頁尾移除、
 * 「TABLE n」下的無框線表格。Python 版另有的有框線表格、圖片擷取、向量圖轉 PNG
 * 需要繪圖指令與點陣化，網頁版不做。
 *
 * 用法：PDF2MD.convert(pdfjsLib, uint8Array, {pages, pageBreaks, keepHeaders}, onProgress)
 *       → Promise<{markdown, pageCount, warnings}>
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.PDF2MD = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ------------------------------------------------------------------------
  // 規則常數（與 converter.py 相同）
  // ------------------------------------------------------------------------
  const CJK_RE = /[⺀-⿿　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;
  const BULLET_CHARS = "•●○◦▪▫■□◆◇‣⁃∙·・\\-–*";
  const BULLET_RE = new RegExp(`^\\s*[${BULLET_CHARS}]\\s+`);
  const BULLET_ONLY_RE = new RegExp(`^\\s*[${BULLET_CHARS}]\\s*$`);
  const ORDERED_RE = /^\s*(\d{1,3}[.)]|\(\d{1,3}\)|（\d{1,3}）)\s+/;
  const SENTENCE_END_RE = /[.!?。！？：:;；」』）)\]"”]$/;
  const PAGE_NUMBER_RE =
    /^(?:[-–—]?\s*\d{1,4}\s*[-–—]?|第\s*\d+\s*頁(?:\s*[/／,，]?\s*共\s*\d+\s*頁)?|page\s*\d+(?:\s*(?:of|\/)\s*\d+)?|\d+\s*\/\s*\d+)$/i;
  const BOLD_FONT_RE = /bold|black|heavy|semibold|demi/i;
  const ITALIC_FONT_RE = /italic|oblique/i;
  const MONO_FONT_RE = /mono|courier|consol|menlo|code/i;
  const CAPTION_RE = /^\s*(?:table|tab\.|表)\s*[\dIVXivx]+/i;
  const FIGURE_RE = /^\s*(?:figure|fig\.|圖)\s*\d+/i;

  const MARGIN_RATIO = 0.08;
  const HEADING_SIZE_RATIO = 1.15;
  const MAX_HEADING_LEVELS = 4;
  const MAX_HEADING_CHARS = 200;
  const INDENT_STEP_PT = 18;
  const COLUMN_GAP_PT = 20;

  // ------------------------------------------------------------------------
  // 文字工具
  // ------------------------------------------------------------------------
  const isCjk = (ch) => !!ch && CJK_RE.test(ch);
  const displayLen = (t) => [...t].reduce((n, ch) => n + (isCjk(ch) ? 2 : 1), 0);

  function joinLines(prev, nxt) {
    prev = prev.replace(/\s+$/, "");
    nxt = nxt.replace(/^\s+/, "");
    if (!prev) return nxt;
    if (!nxt) return prev;
    if (prev.endsWith("-") && prev.length >= 2 && /\p{L}/u.test(prev.at(-2)) && /\p{Ll}/u.test(nxt[0])) {
      const words = prev.split(/\s+/);
      const last = words[words.length - 1];
      if (last.slice(0, -1).includes("-")) return prev + nxt; // 複合詞保留連字號
      return prev.slice(0, -1) + nxt;
    }
    const sep = isCjk(prev.at(-1)) || isCjk(nxt[0]) ? "" : " ";
    if (/(?<![*\\])\*\*$/.test(prev) && /^\*\*(?!\*)/.test(nxt) && !prev.endsWith("***")) {
      return prev.slice(0, -2) + sep + nxt.slice(2);
    }
    return prev + sep + nxt;
  }

  const escapeMd = (t) => t.replace(/([\\`*_])/g, "\\$1");
  const escapeLineStart = (t) => t.replace(/^(\s*)([#>])/, "$1\\$2");

  function renderSpans(spans, plain = false) {
    const merged = [];
    for (const s of spans) {
      if (!s.text) continue;
      const last = merged[merged.length - 1];
      if (last && last.bold === s.bold && last.italic === s.italic && last.mono === s.mono) {
        last.text += s.text;
      } else {
        merged.push({ ...s });
      }
    }
    return merged
      .map((s) => {
        if (plain) return escapeMd(s.text);
        const core = s.text.trim();
        if (!core) return s.text;
        const lead = s.text.slice(0, s.text.length - s.text.replace(/^\s+/, "").length);
        const trail = s.text.slice(s.text.replace(/\s+$/, "").length);
        let out;
        if (s.mono) {
          const tick = core.includes("`") ? "``" : "`";
          out = tick + core + tick;
        } else {
          out = escapeMd(core);
          if (s.bold && s.italic) out = `***${out}***`;
          else if (s.bold) out = `**${out}**`;
          else if (s.italic) out = `*${out}*`;
        }
        return lead + out + trail;
      })
      .join("");
  }

  function parsePageSpec(spec, pageCount) {
    if (!spec || !spec.trim()) return [...Array(pageCount).keys()];
    const pages = [];
    for (let part of spec.split(/[,，]/)) {
      part = part.trim();
      if (!part) continue;
      let start, end;
      if (part.includes("-")) {
        const [a, b] = part.split("-", 2);
        start = a.trim() ? parseInt(a, 10) : 1;
        end = b.trim() ? parseInt(b, 10) : pageCount;
      } else {
        start = end = parseInt(part, 10);
      }
      if (!Number.isFinite(start) || !Number.isFinite(end) || start < 1 || end < start) {
        throw new Error(`頁碼範圍不正確：${part}`);
      }
      for (let p = start; p <= Math.min(end, pageCount); p++) {
        if (!pages.includes(p - 1)) pages.push(p - 1);
      }
    }
    return pages;
  }

  // ------------------------------------------------------------------------
  // Line 物件
  // ------------------------------------------------------------------------
  function mostCommon(counter) {
    let best = null, n = -1;
    for (const [k, v] of counter) if (v > n) { best = k; n = v; }
    return best;
  }

  function makeLine(spans, size, bbox, page) {
    const text = spans.map((s) => s.text).join("");
    const weights = new Map();
    for (const s of spans) weights.set(s.font, (weights.get(s.font) || 0) + s.text.trim().length);
    const font = mostCommon(weights) ?? "";
    const visible = spans.filter((s) => s.text.trim());
    return {
      spans, size, bbox, page, block: 0, text, font,
      key: `${font}|${size}`,
      pure: new Set(visible.map((s) => s.font)).size <= 1,
      allBold: visible.length > 0 && visible.every((s) => s.bold),
      allMono: visible.length > 0 && visible.every((s) => s.mono),
    };
  }

  // ------------------------------------------------------------------------
  // 擷取：pdf.js 的文字片段 → 行 → 區塊
  // ------------------------------------------------------------------------
  async function extractPage(pdfjsLib, page, pageNo) {
    const viewport = page.getViewport({ scale: 1 });
    const content = await page.getTextContent();
    let fontsReady = true;
    try {
      await page.getOperatorList(); // 載入字型物件，才能取得真正的字型名稱（判斷粗體、斜體、等寬）
    } catch (e) {
      fontsReady = false;
    }
    const fontInfo = new Map();
    const fontOf = (id) => {
      if (!fontInfo.has(id)) {
        let name = id;
        try {
          if (fontsReady && page.commonObjs.has(id)) name = page.commonObjs.get(id).name || id;
        } catch (e) { /* 字型未載入時退回 id */ }
        name = String(name).replace(/^[A-Z]{6}\+/, "");
        fontInfo.set(id, {
          font: name,
          bold: BOLD_FONT_RE.test(name),
          italic: ITALIC_FONT_RE.test(name),
          mono: MONO_FONT_RE.test(name),
        });
      }
      return fontInfo.get(id);
    };

    // 文字片段轉成閱讀座標。viewport 已處理頁面的 /Rotate；內容本身旋轉 90° 的橫印頁，
    // 再以頁上最主要的文字方向為準把座標轉正（與 Python 版 _derotate 相同用意）。
    const placed = [];
    const dirs = new Map();
    for (const item of content.items) {
      if (!("str" in item)) continue;
      const tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
      const size = Math.hypot(tx[2], tx[3]);
      const run = Math.hypot(tx[0], tx[1]);
      if (!size || !run) continue;
      const dir = [Math.round(tx[0] / run), Math.round(tx[1] / run)];
      const key = dir.join(",");
      dirs.set(key, (dirs.get(key) || 0) + item.str.length);
      placed.push({ item, tx, size, dir, key });
    }
    const mainKey = mostCommon(dirs) ?? "1,0";
    const [dx, dy] = mainKey.split(",").map(Number);
    const frags = [];
    for (const { item, tx, size, key } of placed) {
      if (key !== mainKey) continue; // 方向不同的零星文字（如橫印頁上的頁首）略過
      const x = tx[4] * dx + tx[5] * dy;
      const y = -tx[4] * dy + tx[5] * dx;
      const width = item.width * viewport.scale;
      frags.push({ str: item.str, size, x0: x, x1: x + width, base: y, ...fontOf(item.fontName) });
    }
    const frameHeight = dx !== 0 ? viewport.height : viewport.width;

    // 片段 → 行（依內容串流順序；換基線、往回跳或大段空白就換行）。
    // 不採用 pdf.js 的 hasEOL：它在每個上標後面都會標記換行。
    const raw = [];
    let cur = null;
    for (const f of frags) {
      // 純空白片段略過：pdf.js 會合成一個寬度填滿整段空隙的空白，讓表格兩欄看起來相連。
      // 字與字之間的空格改由實際間距判斷補上。
      if (!f.str.trim()) continue;
      const sameLine =
        cur &&
        // 上標、下標（文獻編號、作者所屬機構）基線偏移，但仍屬同一行
        Math.abs(f.base - cur.base) < 0.5 * Math.max(f.size, cur.size) &&
        f.x0 > cur.x1 - 0.5 * f.size &&
        f.x0 - cur.x1 < 1.5 * Math.max(f.size, cur.size); // 字距遠大於空白（約 0.25 字級）＝表格的另一欄
      if (!sameLine) {
        cur = { frags: [], base: f.base, size: f.size, x0: f.x0, x1: f.x1 };
        raw.push(cur);
      } else {
        const gap = f.x0 - cur.x1;
        const prev = cur.frags[cur.frags.length - 1];
        const a = prev.str.at(-1), b = f.str[0];
        const needSpace =
          gap > 0.2 * f.size && !/\s/.test(a) && !/\s/.test(b) && !(isCjk(a) && isCjk(b) && gap < 0.6 * f.size);
        if (needSpace) prev.str += " ";
      }
      cur.frags.push({ ...f });
      cur.x1 = Math.max(cur.x1, f.x1);
    }

    let lines = [];
    for (const r of raw) {
      const spans = r.frags.map((f) => ({ text: f.str, bold: f.bold, italic: f.italic, mono: f.mono, font: f.font }));
      if (!spans.map((s) => s.text).join("").trim()) continue;
      const weights = new Map();
      for (const f of r.frags) {
        const s = Math.round(f.size * 2) / 2;
        weights.set(s, (weights.get(s) || 0) + f.str.trim().length);
      }
      const size = mostCommon(weights);
      const maxSize = Math.max(...r.frags.map((f) => f.size));
      const bbox = [r.x0, r.base - 0.85 * maxSize, r.x1, r.base + 0.25 * maxSize];
      lines.push(makeLine(spans, size, bbox, pageNo));
    }

    // 行 → 區塊：緊接在下、左緣對齊（或首行縮排）的行屬同一區塊
    let block = -1, prev = null, blockStart = null;
    for (const ln of lines) {
      let same = false;
      if (prev) {
        const gap = ln.bbox[1] - prev.bbox[3];
        const close = gap >= -0.3 * prev.size && gap < 0.5 * prev.size;
        const aligned = Math.abs(ln.bbox[0] - prev.bbox[0]) <= 2;
        const afterIndent = prev === blockStart && prev.bbox[0] > ln.bbox[0] && prev.bbox[0] - ln.bbox[0] <= 3 * prev.size;
        same = close && (aligned || afterIndent);
      }
      if (!same) { block += 1; blockStart = ln; }
      ln.block = block;
      prev = ln;
    }
    // 區塊依位置排序（由上而下、由左而右），區塊內保留原順序
    const first = new Map();
    for (const ln of lines) if (!first.has(ln.block)) first.set(ln.block, [ln.bbox[1], ln.bbox[0]]);
    lines = lines
      .map((ln, i) => [ln, i])
      .sort((a, b) => {
        const fa = first.get(a[0].block), fb = first.get(b[0].block);
        return fa[0] - fb[0] || fa[1] - fb[1] || a[1] - b[1];
      })
      .map(([ln]) => ln);

    return { lines, height: frameHeight, textItems: placed.length };
  }

  // ------------------------------------------------------------------------
  // 頁首頁尾
  // ------------------------------------------------------------------------
  const marginKey = (ln) => ln.text.trim().replace(/\d+/g, "#");
  const inMargin = (ln, h) => ln.bbox[3] <= h * MARGIN_RATIO || ln.bbox[1] >= h * (1 - MARGIN_RATIO);

  function repeatedMarginKeys(pageLines, heights) {
    const pages = Object.keys(pageLines);
    if (pages.length < 2) return new Set();
    const counter = new Map();
    for (const p of pages) {
      const keys = new Set(pageLines[p].filter((ln) => inMargin(ln, heights[p])).map(marginKey));
      for (const k of keys) counter.set(k, (counter.get(k) || 0) + 1);
    }
    const threshold = Math.max(2, Math.floor((pages.length + 1) / 2));
    return new Set([...counter].filter(([, n]) => n >= threshold).map(([k]) => k));
  }

  // ------------------------------------------------------------------------
  // 無框線表格（「TABLE n」標題下方）
  // ------------------------------------------------------------------------
  function captionTables(lines, bodySize) {
    const ordered = [...lines].sort((a, b) => a.bbox[1] - b.bbox[1] || a.bbox[0] - b.bbox[0]);
    const taken = new Set();
    const tables = [];
    ordered.forEach((cap, i) => {
      if (taken.has(cap) || !CAPTION_RE.test(cap.text)) return;
      const region = [];
      let bottom = cap.bbox[3];
      for (const ln of ordered.slice(i + 1)) {
        if (ln.bbox[1] < cap.bbox[3] - 1) continue;
        if (ln.size >= bodySize - 0.25 || CAPTION_RE.test(ln.text) || FIGURE_RE.test(ln.text)) break;
        const gap = ln.bbox[1] - bottom;
        if (gap > ln.size * (region.length ? 2.5 : 5)) break;
        region.push(ln);
        bottom = Math.max(bottom, ln.bbox[3]);
      }
      const table = layoutTable(region);
      if (!table) return;
      table.used.forEach((ln) => taken.add(ln));
      tables.push({ y: Math.min(...table.used.map((ln) => ln.bbox[1])), md: table.md });
    });
    return { remaining: lines.filter((ln) => !taken.has(ln)), tables };
  }

  function layoutTable(region) {
    if (region.length < 2) return null;
    const xs = region.map((ln) => ln.bbox[0]).sort((a, b) => a - b);
    const starts = [xs[0]];
    for (let i = 1; i < xs.length; i++) if (xs[i] - xs[i - 1] > COLUMN_GAP_PT) starts.push(xs[i]);
    if (starts.length < 2) return null;
    const colOf = (ln) => {
      let c = 0;
      starts.forEach((x, i) => { if (x <= ln.bbox[0] + 2) c = i; });
      return c;
    };

    const used = [];
    for (const ln of region) {
      const c = colOf(ln);
      if (c + 1 < starts.length && ln.bbox[2] > starts[c + 1] + 2) break; // 橫跨多欄＝註腳
      used.push(ln);
    }
    if (used.length < 2 || new Set(used.map(colOf)).size < 2) return null;

    const cells = new Map();
    starts.forEach((_, c) => {
      const colLines = used.filter((ln) => colOf(ln) === c);
      const hanging = new Set(colLines.map((ln) => Math.round(ln.bbox[0]))).size >= 2;
      const out = [];
      let current = null;
      for (const ln of colLines) {
        const lastLine = current && current.lines[current.lines.length - 1];
        const close = current && ln.bbox[1] - lastLine.bbox[3] < 0.8 * ln.size;
        const sameFont = current && ln.font === current.start.font;
        const cont = hanging ? current && ln.bbox[0] > current.start.bbox[0] + 2 : c !== 0;
        if (current && close && sameFont && cont) current.lines.push(ln);
        else { current = { start: ln, lines: [ln] }; out.push(current); }
      }
      cells.set(c, out);
    });

    const rowStarts = (cells.get(0) || []).map((cell) => cell.start.bbox[1]);
    if (!rowStarts.length) return null;
    const rowOf = (y) => {
      let idx = 0;
      rowStarts.forEach((ry, i) => { if (ry <= y + 2) idx = i; });
      return idx;
    };
    const grid = rowStarts.map(() => starts.map(() => ""));
    for (const [c, colCells] of cells) {
      for (const cell of colCells) {
        let text = "";
        for (const ln of cell.lines) text = joinLines(text, renderSpans(ln.spans));
        text = text.trim().replace(/\|/g, "\\|");
        const r = rowOf(cell.start.bbox[1]);
        grid[r][c] = grid[r][c] ? `${grid[r][c]}<br>${text}` : text;
      }
    }
    if (grid.length < 2) return null;

    const fontsOf = (r) => [...cells.keys()].map((c) => {
      const hit = cells.get(c).find((cell) => rowOf(cell.start.bbox[1]) === r);
      return hit ? hit.start.font : null;
    });
    const firstFonts = fontsOf(0);
    const rest = grid.slice(1).map((_, i) => fontsOf(i + 1));
    const isHeader = firstFonts.every(Boolean) &&
      firstFonts.every((f, c) => rest.every((other) => !other[c] || other[c] !== f));
    const header = isHeader ? grid[0] : starts.map(() => "");
    const body = isHeader ? grid.slice(1) : grid;
    const md = [`| ${header.join(" | ")} |`, `|${header.map(() => " --- ").join("|")}|`]
      .concat(body.map((r) => `| ${r.join(" | ")} |`));
    return { md: md.join("\n"), used };
  }

  // ------------------------------------------------------------------------
  // 結構推論
  // ------------------------------------------------------------------------
  function bodySizeOf(lines) {
    const sizes = new Map();
    for (const ln of lines) sizes.set(ln.size, (sizes.get(ln.size) || 0) + ln.text.trim().length);
    return mostCommon(sizes) ?? 0;
  }

  function headingLevels(lines) {
    if (!lines.length) return { body: 0, sizeLevels: new Map(), fontLevels: new Map() };
    const body = bodySizeOf(lines);
    const big = [...new Set(lines.map((ln) => ln.size))]
      .filter((s) => s >= body * HEADING_SIZE_RATIO && s - body >= 1)
      .sort((a, b) => b - a);
    const sizeLevels = new Map(big.map((s, i) => [s, Math.min(i + 1, MAX_HEADING_LEVELS)]));

    const fonts = new Map();
    for (const ln of lines) if (ln.size === body) fonts.set(ln.font, (fonts.get(ln.font) || 0) + ln.text.trim().length);
    const bodyFont = mostCommon(fonts) ?? "";

    const stats = new Map();
    let prev = null, run = null;
    lines.forEach((ln, idx) => {
      if (!stats.has(ln.key)) stats.set(ln.key, { lines: 0, pure: 0, mono: 0, runs: [], first: idx, size: ln.size, font: ln.font });
      const st = stats.get(ln.key);
      st.lines += 1;
      st.pure += ln.pure ? 1 : 0;
      st.mono += ln.allMono ? 1 : 0;
      const sameBlock = prev && prev.page === ln.page && prev.block === ln.block;
      if (sameBlock && prev.key === ln.key) {
        run.text = joinLines(run.text, ln.text);
      } else {
        run = { text: ln.text.trim(), placed: !sameBlock || prev.font !== bodyFont };
        st.runs.push(run);
      }
      prev = ln;
    });

    const candidates = [];
    for (const [key, st] of stats) {
      if (st.font === bodyFont || sizeLevels.has(st.size) || st.size < body - 0.25 || st.mono) continue;
      const runs = st.runs;
      const lengths = runs.map((r) => displayLen(r.text)).sort((a, b) => a - b);
      if (
        st.pure >= 0.8 * st.lines &&
        lengths[Math.floor(lengths.length / 2)] <= 150 &&
        lengths.filter((n) => n <= 200).length >= 0.8 * runs.length &&
        runs.filter((r) => /[.。!！]$/.test(r.text)).length <= 0.3 * runs.length &&
        runs.filter((r) => r.placed).length >= 0.8 * runs.length
      ) {
        candidates.push([st.first, key]);
      }
    }
    const base = Math.max(0, ...sizeLevels.values());
    const fontLevels = new Map(
      candidates.sort((a, b) => a[0] - b[0]).map(([, key], i) => [key, Math.min(base + i + 1, 6)])
    );
    return { body, sizeLevels, fontLevels };
  }

  function dropPrefix(spans, n) {
    const out = [];
    for (const s of spans) {
      if (n <= 0) out.push(s);
      else if (s.text.length <= n) n -= s.text.length;
      else { out.push({ ...s, text: s.text.slice(n) }); n = 0; }
    }
    return out;
  }

  function linesToItems(lines, body, sizeLevels, fontLevels) {
    const boldLevel = Math.min(Math.max(0, ...sizeLevels.values(), ...fontLevels.values()) + 1, 6);
    const items = [];
    let pendingBullet = null;

    const boundary = (a, b) => {
      if (a.page !== b.page || a.block !== b.block) return true;
      if ((fontLevels.has(a.key) || fontLevels.has(b.key)) && a.key !== b.key) return true;
      return (sizeLevels.has(a.size) || sizeLevels.has(b.size)) && a.size !== b.size;
    };
    const groups = [];
    for (const ln of lines) {
      const g = groups[groups.length - 1];
      if (g && !boundary(g[g.length - 1], ln)) g.push(ln);
      else groups.push([ln]);
    }

    const headingText = (group) => group.reduce((t, ln) => joinLines(t, renderSpans(ln.spans, true)), "");

    for (const group of groups) {
      const first = group[0];
      const plain = group.map((ln) => ln.text).join("").trim();
      const base = { size: first.size, page: first.page, x0: first.bbox[0], y: first.bbox[1] };

      if (group.every((ln) => ln.allMono)) {
        items.push({ kind: "code", text: group.map((ln) => ln.text.replace(/\s+$/, "")).join("\n"), ...base });
        continue;
      }
      if (sizeLevels.has(first.size) && group.every((ln) => ln.size === first.size) && displayLen(plain) <= MAX_HEADING_CHARS) {
        items.push({ kind: "heading", text: headingText(group), level: sizeLevels.get(first.size), ...base });
        continue;
      }
      if (fontLevels.has(first.key) && group.every((ln) => ln.key === first.key) && displayLen(plain) <= MAX_HEADING_CHARS) {
        items.push({ kind: "heading", text: headingText(group), level: fontLevels.get(first.key), ...base });
        continue;
      }
      if (
        group.length <= 2 && group.every((ln) => ln.allBold) && plain.length <= 60 &&
        !/[.。]$/.test(plain) && !BULLET_RE.test(plain) && !BULLET_ONLY_RE.test(plain) &&
        Math.abs(first.size - body) < 1
      ) {
        items.push({ kind: "heading", text: headingText(group), level: boldLevel, ...base });
        continue;
      }

      let current = null;
      for (const ln of group) {
        const rawText = ln.text;
        const rendered = renderSpans(ln.spans);
        const lnBase = { size: ln.size, page: ln.page, x0: ln.bbox[0], y: ln.bbox[1] };
        if (BULLET_ONLY_RE.test(rawText)) { pendingBullet = ln; continue; }
        if (pendingBullet) {
          current = { kind: "bullet", text: rendered.trim(), ...lnBase, x0: pendingBullet.bbox[0], y: pendingBullet.bbox[1] };
          items.push(current);
          pendingBullet = null;
        } else if (BULLET_RE.test(rawText)) {
          const markerLen = rawText.match(BULLET_RE)[0].length;
          current = { kind: "bullet", text: renderSpans(dropPrefix(ln.spans, markerLen)).trim(), ...lnBase };
          items.push(current);
        } else if (ORDERED_RE.test(rawText)) {
          current = { kind: "ordered", text: rendered.trim(), ...lnBase };
          items.push(current);
        } else if (!current) {
          current = { kind: "para", text: rendered.trim(), ...lnBase };
          items.push(current);
        } else {
          current.text = joinLines(current.text, rendered);
        }
      }
    }
    return items;
  }

  function insertByPosition(textItems, extras) {
    const out = [...textItems];
    for (const ex of [...extras].sort((a, b) => a.y - b.y)) {
      let idx = out.findIndex((it) => it.kind !== "table" && it.y > ex.y);
      if (idx < 0) idx = out.length;
      out.splice(idx, 0, ex);
    }
    return out;
  }

  function mergeAcrossBreaks(items) {
    const merged = [];
    for (const it of items) {
      const prev = merged[merged.length - 1];
      if (
        prev && prev.kind === "para" && it.kind === "para" && Math.abs(prev.size - it.size) < 0.5 &&
        prev.page !== it.page && !SENTENCE_END_RE.test(prev.text)
      ) {
        prev.text = joinLines(prev.text, it.text);
        continue;
      }
      merged.push(it);
    }
    return merged;
  }

  function compactHeadingLevels(items) {
    const used = [...new Set(items.filter((it) => it.kind === "heading").map((it) => it.level))].sort((a, b) => a - b);
    const remap = new Map(used.map((l, i) => [l, i + 1]));
    for (const it of items) if (it.kind === "heading") it.level = remap.get(it.level);
    return items;
  }

  function renderItems(items) {
    const out = [];
    let i = 0;
    while (i < items.length) {
      const it = items[i];
      if (it.kind === "bullet" || it.kind === "ordered") {
        const run = [it];
        i += 1;
        while (i < items.length && (items[i].kind === "bullet" || items[i].kind === "ordered")) {
          if (items[i].kind !== it.kind && items[i].x0 <= it.x0 + 1) break;
          run.push(items[i]);
          i += 1;
        }
        const baseX = Math.min(...run.map((r) => r.x0));
        out.push(run.map((r) => {
          const indent = "  ".repeat(Math.min(3, Math.floor((r.x0 - baseX) / INDENT_STEP_PT)));
          return indent + (r.kind === "bullet" ? "- " : "") + r.text;
        }).join("\n"));
        continue;
      }
      if (it.kind === "heading") out.push(`${"#".repeat(it.level)} ${it.text}`);
      else if (it.kind === "para") out.push(escapeLineStart(it.text));
      else if (it.kind === "code") {
        const fence = it.text.includes("```") ? "````" : "```";
        out.push(`${fence}\n${it.text}\n${fence}`);
      } else out.push(it.text);
      i += 1;
    }
    return out.join("\n\n").trim() + "\n";
  }

  function formatPages(pages) {
    const ranges = [];
    let start = pages[0], prev = pages[0];
    for (const p of pages.slice(1)) {
      if (p === prev + 1) { prev = p; continue; }
      ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
      start = prev = p;
    }
    ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
    return ranges.join("、");
  }

  // ------------------------------------------------------------------------
  // 對外 API
  // ------------------------------------------------------------------------
  async function convert(pdfjsLib, data, options = {}, onProgress = () => {}) {
    const {
      pages = "", pageBreaks = false, keepHeaders = false, detectTables = true, password, cMapUrl, CMapReaderFactory,
    } = options;
    // 未內嵌的中日韓字型需要 pdf.js 的 CMap 檔才能把字形對應回文字：
    // 給 cMapUrl（CMap 資料夾網址），或給自訂的 CMapReaderFactory（例如從內嵌資料讀取）。
    const doc = await pdfjsLib.getDocument({
      data, password, isEvalSupported: false,
      ...(cMapUrl ? { cMapUrl, cMapPacked: true } : {}),
      ...(CMapReaderFactory ? { CMapReaderFactory, useWorkerFetch: false } : {}),
    }).promise;
    const warnings = [];
    try {
      const pageNos = parsePageSpec(pages, doc.numPages);
      const pageLines = {}, heights = {};
      const scanned = [];
      for (const [n, p] of pageNos.entries()) {
        const page = await doc.getPage(p + 1);
        const { lines, height, textItems } = await extractPage(pdfjsLib, page, p);
        pageLines[p] = lines;
        heights[p] = height;
        if (!textItems) scanned.push(p + 1);
        page.cleanup();
        onProgress({ done: n + 1, total: pageNos.length });
      }

      if (!keepHeaders) {
        const repeated = repeatedMarginKeys(pageLines, heights);
        for (const p of pageNos) {
          pageLines[p] = pageLines[p].filter(
            (ln) => !(inMargin(ln, heights[p]) && (repeated.has(marginKey(ln)) || PAGE_NUMBER_RE.test(ln.text.trim())))
          );
        }
      }

      const pageTables = {};
      if (detectTables) {
        const bodySize = bodySizeOf(pageNos.flatMap((p) => pageLines[p]));
        for (const p of pageNos) {
          const found = captionTables(pageLines[p], bodySize);
          pageLines[p] = found.remaining;
          pageTables[p] = found.tables;
        }
      }

      const { body, sizeLevels, fontLevels } = headingLevels(pageNos.flatMap((p) => pageLines[p]));
      let items = [];
      for (const p of pageNos) {
        if (pageBreaks) items.push({ kind: "pagebreak", text: `<!-- page ${p + 1} -->`, page: p, y: 0 });
        const textItems = linesToItems(pageLines[p], body, sizeLevels, fontLevels);
        const extras = (pageTables[p] || []).map((t) => ({ kind: "table", text: t.md, page: p, y: t.y }));
        items.push(...insertByPosition(textItems, extras));
      }
      items = mergeAcrossBreaks(compactHeadingLevels(items));
      const markdown = renderItems(items);

      if (scanned.length) {
        warnings.push(`第 ${formatPages(scanned)} 頁沒有文字層（可能是掃描影像），無法擷取文字，需先做 OCR。`);
      }
      if (!markdown.trim()) warnings.push("沒有擷取到任何內容。");
      return { markdown, pageCount: doc.numPages, warnings };
    } finally {
      await doc.destroy();
    }
  }

  return { convert, joinLines, parsePageSpec, escapeMd };
});

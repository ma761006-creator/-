#!/usr/bin/env node
// 產生在 Claude 上發佈用的檔案到 pdf2md/web/dist/：
//   index.html      取 index.html 中 ARTIFACT 標記之間的片段（發佈時會自動包上 <html>/<head>/<body>）
//   pdf2md-core.js  轉換核心
//   cmaps.js        pdf.js 的 CMap（二進位）以 base64 內嵌成腳本：發佈平台不提供二進位檔
// 需先 npm install --prefix pdf2md/web
const fs = require("fs");
const path = require("path");

const here = __dirname;
const dist = path.join(here, "dist");
fs.mkdirSync(dist, { recursive: true });

const html = fs.readFileSync(path.join(here, "index.html"), "utf8");
const between = (a, b) => html.split(a)[1].split(b)[0].trim();
const fragment = between("<!-- ARTIFACT:START -->", "<!-- ARTIFACT:HEAD-END -->") + "\n" +
  between("<!-- ARTIFACT:BODY -->", "<!-- ARTIFACT:END -->") + "\n";
fs.writeFileSync(path.join(dist, "index.html"), fragment);
fs.copyFileSync(path.join(here, "pdf2md-core.js"), path.join(dist, "pdf2md-core.js"));

const cmapDir = path.join(path.dirname(require.resolve("pdfjs-dist/package.json", { paths: [here] })), "cmaps");
const maps = {};
for (const f of fs.readdirSync(cmapDir).filter((f) => f.endsWith(".bcmap")).sort()) {
  maps[f.replace(/\.bcmap$/, "")] = fs.readFileSync(path.join(cmapDir, f)).toString("base64");
}
fs.writeFileSync(path.join(dist, "cmaps.js"), `window.PDF2MD_CMAPS=${JSON.stringify(maps)};\n`);
console.log(`dist/: index.html, pdf2md-core.js, cmaps.js (${Object.keys(maps).length} CMaps)`);

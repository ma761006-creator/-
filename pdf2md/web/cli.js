#!/usr/bin/env node
// 以 Node 執行網頁版核心：node pdf2md/web/cli.js 檔案.pdf ['{"pages":"1-3"}']
// 供測試比對 Python 版與網頁版的輸出；需先 npm install --prefix pdf2md/web
// pdf.js 在 Node 缺少 canvas 時會印出警告，與文字擷取無關
const warn = console.warn;
console.warn = (...args) => (String(args[0]).startsWith("Warning:") ? undefined : warn(...args));
const log = console.log;
console.log = (...args) => (String(args[0]).startsWith("Warning:") ? undefined : log(...args));
const fs = require("fs");
const path = require("path");
const pdfjsLib = require(require.resolve("pdfjs-dist/legacy/build/pdf.js", { paths: [__dirname] }));
const core = require(path.join(__dirname, "pdf2md-core.js"));

const [file, opts] = process.argv.slice(2);

core
  .convert(pdfjsLib, new Uint8Array(fs.readFileSync(file)), {
    cMapUrl: path.join(path.dirname(require.resolve("pdfjs-dist/package.json", { paths: [__dirname] })), "cmaps") + "/",
    ...JSON.parse(opts || "{}"),
  })
  .then((r) => {
    process.stdout.write(JSON.stringify(r));
  })
  .catch((e) => {
    process.stderr.write(String((e && e.message) || e));
    process.exit(1);
  });

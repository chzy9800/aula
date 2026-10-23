// pdf.js 口径基准：用 aula 自带的 vendored pdf.js 解析同一份 PDF，
// 复刻 reader.js 的 groupBlocks 逻辑，输出段落数与段落文本，用于和 PDFKit 对比。
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const PDF = "/Users/cheng/WorkBuddy/2026-10-21-16-19-53/aula/aula/static/vendor/pdf.min.mjs";
const WORKER = "/Users/cheng/WorkBuddy/2026-10-21-16-19-53/aula/aula/static/vendor/pdf.worker.min.mjs";
const FILE = process.argv[2];
const PAGE_LIMIT = Number(process.argv[3] || 5);

const pdfjs = await import(pathToFileURL(PDF).href);
pdfjs.GlobalWorkerOptions.workerSrc = pathToFileURL(WORKER).href;

function joinParts(parts) {
  let out = "";
  for (const part of parts) {
    const piece = part.text;
    if (!out) { out = piece; continue; }
    const needSpace = !/[\s-]$/.test(out) && !/^[\s.,;:!?)\]%]/.test(piece);
    out += (needSpace ? " " : "") + piece;
  }
  return out.replace(/\s{2,}/g, " ").trim();
}

// 与 reader.js groupBlocks 逐行同口径
function groupBlocks(items, viewport) {
  const lines = [];
  for (const item of items) {
    if (!item.str || !item.str.trim()) continue;
    const transform = item.transform;
    const x = transform[4];
    const y = viewport.height - transform[5];
    const height = Math.abs(transform[3]) || item.height || 10;
    let line = lines.find((c) => Math.abs(c.y - y) < height * 0.5);
    if (!line) { line = { y, height, parts: [] }; lines.push(line); }
    line.parts.push({ x, text: item.str });
    line.height = Math.max(line.height, height);
  }
  lines.sort((a, b) => a.y - b.y);
  for (const line of lines) {
    line.parts.sort((a, b) => a.x - b.x);
    line.text = joinParts(line.parts);
  }
  const lineHeights = lines.map((l) => l.height).sort((a, b) => a - b);
  const medianLineHeight = lineHeights[Math.floor(lineHeights.length / 2)] || 10;
  for (const line of lines) line.isHeading = line.height > medianLineHeight * 1.25;

  const blocks = [];
  for (const line of lines) {
    const last = blocks[blocks.length - 1];
    const gapRatio = last ? (line.y - last.lastY) / Math.max(line.height, last.height) : 99;
    const scaleChanged = last ? Math.abs(line.height - last.height) / Math.max(line.height, last.height) > 0.25 : false;
    const gapLimit = last && last.isHeading ? 2.05 : 1.62;
    if (last && gapRatio < gapLimit && !scaleChanged) {
      last.text += (/[。.!?;:：；]$/.test(last.text) ? "\n" : " ") + line.text;
      last.lastY = line.y;
    } else {
      blocks.push({ text: line.text, y: line.y, lastY: line.y, height: line.height, heading: !!line.isHeading, isHeading: !!line.isHeading });
    }
  }
  for (const b of blocks) b.heading = !!b.isHeading;
  for (let i = blocks.length - 1; i > 0; i -= 1) {
    const cur = blocks[i], prev = blocks[i - 1];
    if (cur.heading && prev.heading && cur.text.trim().length < 34) {
      prev.text = `${prev.text} ${cur.text}`.trim();
      prev.lastY = cur.lastY;
      blocks.splice(i, 1);
    }
  }
  return blocks.filter((b) => b.text.trim().length > 1);
}

const data = new Uint8Array(fs.readFileSync(FILE));
const doc = await pdfjs.getDocument({ data, useSystemFonts: true }).promise;
console.log("pages_total:", doc.numPages);
let totalChars = 0, totalLines = 0, totalParas = 0;
const perPage = [];
const firstParas = [];
for (let p = 1; p <= Math.min(PAGE_LIMIT, doc.numPages); p += 1) {
  const page = await doc.getPage(p);
  const viewport = page.getViewport({ scale: 1 });
  const tc = await page.getTextContent({ normalizeWhitespace: true });
  const blocks = groupBlocks(tc.items, viewport);
  const chars = blocks.reduce((a, b) => a + b.text.length, 0);
  totalChars += chars; totalParas += blocks.length;
  perPage.push([p - 1, tc.items.length, blocks.length, chars]);
  if (p === 1) for (const b of blocks.slice(0, 6)) firstParas.push(b.text);
}
console.log("chars:", totalChars, "paras:", totalParas);
console.log("avg_chars_per_para:", Math.round(totalChars / totalParas));
console.log("--- 首页前 6 段（pdf.js 口径）---");
firstParas.forEach((t, i) => console.log(`[${i}] ${t.length > 110 ? t.slice(0, 110) + "…" : t}`));
console.log("--- 逐页 ---");
for (const [i, items, blocks, chars] of perPage) console.log(`page ${i}: items=${items} paras=${blocks} chars=${chars}`);

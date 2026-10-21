/* aula 讲义工作区前端
 *
 * 三件事：
 *   1. pdf.js 渲染原页（懒加载），同时抽出文本、按行聚成段落
 *   2. 段落送到后端翻译，译文按段落贴在原页右侧，跟着同一个滚动容器走
 *   3. 圈选区域 → 取该区域文字 + 裁图 → 问模型 → 答案钉在原文位置
 *
 * 数据留在浏览器：笔记、手写、问答都存在 localStorage，按文件指纹隔离。
 */

import * as pdfjsLib from "/static/vendor/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.min.mjs";

const $ = (id) => document.getElementById(id);

const state = {
  pdf: null,
  docId: "",
  fileName: "",
  zoom: 1,
  mode: "read",
  pageCount: 0,
  pages: [],          // { pageNum, blocks, el, canvas, ink, rendered, translated, translatePending }
  visiblePages: new Set(),
  pins: [],
  ink: {},            // pageNum -> strokes[]
  notes: "",
  engine: "",
  selection: null,    // 当前圈选 { pageNum, rect, text, image }
  outline: null,      // 大纲树 [{display_title, search_anchor, page_index, difficulty_score, subtopics}]
  terms: [],          // 抽取到的术语 [{term, translation, note, page_index}]
  vocab: [],          // 词库（收藏过的术语，按讲义指纹存本地）
  chat: null,         // 当前圈选对应的连续对话 { key, selection, pageNum, history: [{role, content}] }
  mistakes: [],       // 错题记录 [{id, where, question, answer, note, status, createdAt}]
  inkStroke: null,
  pinPop: null,
};

const DPR = Math.min(window.devicePixelRatio || 1, 2);
const BASE_SCALE = 1.12;

/* ---------- 工具 ---------- */

function toast(message, kind = "info") {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (kind === "error" ? " error" : "");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add("hidden"), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

function fingerprint(name, size) {
  let hash = 2166136261;
  const seed = `${name}:${size}`;
  for (let i = 0; i < seed.length; i++) {
    hash ^= seed.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function store(key, value) {
  const full = `aula:reader:v1:${state.docId}:${key}`;
  try {
    if (value === undefined) {
      const raw = localStorage.getItem(full);
      return raw ? JSON.parse(raw) : null;
    }
    localStorage.setItem(full, JSON.stringify(value));
  } catch (_) {}
  return null;
}

function fmtTime(iso) {
  const date = new Date(iso);
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/* ---------- 打开 PDF ---------- */

async function openFile(file) {
  if (!file) return;
  const buffer = await file.arrayBuffer();
  await openBuffer(buffer, file.name, file.size);
}

async function openBuffer(buffer, name, size) {
  state.docId = fingerprint(name, size);
  state.fileName = name.replace(/\.pdf$/i, "");
  state.pdf = await pdfjsLib.getDocument({ data: buffer }).promise;
  state.pageCount = state.pdf.numPages;
  state.pages = [];
  state.visiblePages.clear();
  state.pins = store("pins") || [];
  state.ink = store("ink") || {};
  state.notes = store("note") || "";
  $("noteInput").value = state.notes;
  state.outline = store("outline");
  renderOutline();
  state.terms = [];
  state.vocab = store("vocab") || [];
  renderVocab();
  state.mistakes = store("mistakes") || [];
  renderMistakes();
  $("termsState").textContent = state.terms.length ? `${state.terms.length} 个` : "—";
  $("outlineState").textContent = state.outline ? "已加载" : "未生成";
  $("empty").classList.add("hidden");
  $("pages").innerHTML = "";
  $("pageLabel").textContent = `1 / ${state.pageCount}`;
  renderPinsList();

  for (let pageNum = 1; pageNum <= state.pageCount; pageNum++) {
    await buildPage(pageNum);
  }
  observePages();
}

async function buildPage(pageNum) {
  const page = await state.pdf.getPage(pageNum);
  const viewport = page.getViewport({ scale: BASE_SCALE });
  const width = Math.min(Math.round(viewport.width), 660);

  const section = document.createElement("section");
  section.className = "page";
  section.dataset.page = String(pageNum);

  const paper = document.createElement("div");
  paper.className = "paper";
  // 先占位，等进入视口再真正渲染
  paper.style.width = `${width}px`;
  paper.style.height = `${Math.round(viewport.height)}px`;

  const canvas = document.createElement("canvas");
  canvas.className = "pdf";
  paper.appendChild(canvas);

  const ink = document.createElement("canvas");
  ink.className = "ink";
  paper.appendChild(ink);

  const pins = document.createElement("div");
  pins.className = "pins";
  paper.appendChild(pins);

  const trans = document.createElement("div");
  trans.className = "trans";
  trans.innerHTML = `<div class="trans-head"><span>第 ${pageNum} 页译文</span><span class="dim">等待翻译</span></div>`;

  section.appendChild(paper);
  section.appendChild(trans);
  $("pages").appendChild(section);

  const record = {
    pageNum,
    page,
    viewport,
    el: section,
    paper,
    canvas,
    ink,
    pinsEl: pins,
    transEl: trans,
    blocks: [],
    rendered: false,
    translated: false,
  };
  state.pages.push(record);

  const textContent = await page.getTextContent({ normalizeWhitespace: true });
  record.blocks = groupBlocks(textContent.items, viewport);
  record.text = record.blocks.map((b) => b.text).join("\n");
  drawPins(record);
  prepareInk(record);
  return record;
}

/* ---------- 文本 → 段落 ---------- */

function groupBlocks(items, viewport) {
  const lines = [];
  for (const item of items) {
    if (!item.str || !item.str.trim()) continue;
    const transform = item.transform;
    const x = transform[4];
    const y = viewport.height - transform[5];   // 统一成屏幕坐标
    const height = Math.abs(transform[3]) || item.height || 10;
    let line = lines.find((candidate) => Math.abs(candidate.y - y) < height * 0.5);
    if (!line) {
      line = { y, height, parts: [] };
      lines.push(line);
    }
    line.parts.push({ x, text: item.str });
    line.height = Math.max(line.height, height);
  }

  lines.sort((a, b) => a.y - b.y);
  for (const line of lines) {
    line.parts.sort((a, b) => a.x - b.x);
    line.text = joinParts(line.parts);
  }

  // 先判断哪些行是大字（标题），标题的多行要能合回同一块
  const lineHeights = lines.map((line) => line.height).sort((a, b) => a - b);
  const medianLineHeight = lineHeights[Math.floor(lineHeights.length / 2)] || 10;
  for (const line of lines) line.isHeading = line.height > medianLineHeight * 1.25;

  // 相邻行合并成段的条件要同时满足：行距够紧、字号没变。
  // 只判行距会把标题、副标题、正文粘成一大块，译文跟着变成一锅粥。
  const blocks = [];
  for (const line of lines) {
    const last = blocks[blocks.length - 1];
    const gapRatio = last ? (line.y - last.lastY) / Math.max(line.height, last.height) : 99;
    // 字号变化超过 25% 视为换层级（标题、副标题、图注），必须断开
    const scaleChanged = last ? Math.abs(line.height - last.height) / Math.max(line.height, last.height) > 0.25 : false;
    // 行内行距通常 1.4–1.5 倍字号，段间 1.7 以上；标题行距更松，放宽到 2.05
    const gapLimit = last && last.isHeading ? 2.05 : 1.62;
    if (last && gapRatio < gapLimit && !scaleChanged) {
      last.text += (/[。.!?;:：；]$/.test(last.text) ? "\n" : " ") + line.text;
      last.lastY = line.y;
    } else {
      blocks.push({
        text: line.text,
        y: line.y,
        lastY: line.y,
        height: line.height,
        translated: "",
        heading: Boolean(line.isHeading),
        isHeading: Boolean(line.isHeading),
      });
    }
  }

  for (const block of blocks) block.heading = Boolean(block.isHeading);

  // 标题折行被拆开时（第二行常常只有一两个词），并回上一块
  for (let i = blocks.length - 1; i > 0; i -= 1) {
    const current = blocks[i];
    const previous = blocks[i - 1];
    if (current.heading && previous.heading && current.text.trim().length < 34) {
      previous.text = `${previous.text} ${current.text}`.trim();
      previous.lastY = current.lastY;
      blocks.splice(i, 1);
    }
  }
  return blocks.filter((block) => block.text.trim().length > 1);
}

function joinParts(parts) {
  let out = "";
  for (const part of parts) {
    const piece = part.text;
    if (!out) {
      out = piece;
      continue;
    }
    const needSpace = !/[\s-]$/.test(out) && !/^[\s.,;:!?)\]%]/.test(piece);
    out += (needSpace ? " " : "") + piece;
  }
  return out.replace(/\s{2,}/g, " ").trim();
}

/* ---------- 渲染 ---------- */

function observePages() {
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const pageNum = Number(entry.target.dataset.page);
        const record = state.pages[pageNum - 1];
        if (!record) continue;
        if (entry.isIntersecting) {
          state.visiblePages.add(pageNum);
          if (!record.rendered) renderCanvas(record);
          renderTranslation(record);
        } else {
          state.visiblePages.delete(pageNum);
        }
        updatePageLabel();
      }
    },
    { root: $("reader"), rootMargin: "800px 0px" }
  );
  for (const record of state.pages) observer.observe(record.el);
}

async function renderCanvas(record) {
  if (record.rendered) return;
  const scale = BASE_SCALE * state.zoom;
  const viewport = record.page.getViewport({ scale });
  record.canvas.width = Math.round(viewport.width * DPR);
  record.canvas.height = Math.round(viewport.height * DPR);
  record.canvas.style.width = `${Math.round(viewport.width)}px`;
  record.canvas.style.height = `${Math.round(viewport.height)}px`;
  record.paper.style.width = `${Math.round(viewport.width)}px`;
  record.paper.style.height = `${Math.round(viewport.height)}px`;
  record.ink.width = record.canvas.width;
  record.ink.height = record.canvas.height;
  record.ink.style.width = record.canvas.style.width;
  record.ink.style.height = record.canvas.style.height;

  const context = record.canvas.getContext("2d");
  await record.page.render({ canvasContext: context, viewport, transform: DPR !== 1 ? [DPR, 0, 0, DPR, 0, 0] : null }).promise;
  record.rendered = true;
  redrawInk(record);
}

function renderTranslation(record) {
  if (record.translated || record.translatePending) return;
  if (!record.blocks.length) {
    record.transEl.innerHTML = `<div class="trans-head"><span>第 ${record.pageNum} 页译文</span></div><div class="trans-empty">这一页没有可提取的文字（可能是扫描件）。</div>`;
    record.translated = true;
    return;
  }
  record.translatePending = true;
  const head = `<div class="trans-head"><span>第 ${record.pageNum} 页译文</span><span class="dim" data-role="state">翻译中…</span></div>`;
  const body = record.blocks
    .map(
      (block, index) =>
        `<div class="block loading${block.heading ? " is-heading" : ""}" data-block="${index}"><div class="src">${escapeHtml(block.text.slice(0, 90))}${block.text.length > 90 ? "…" : ""}</div><div class="dst"></div></div>`
    )
    .join("");
  record.transEl.innerHTML = head + body;
  translateRecord(record);
}

async function translateRecord(record) {
  const cached = store(`trans:${state.sourceLang}:${state.targetLang}`) || {};
  const blocks = record.blocks;
  try {
    const result = await api("/api/reader/translate", {
      method: "POST",
      body: JSON.stringify({
        blocks: blocks.map((b) => b.text),
        source: state.sourceLang,
        target: state.targetLang,
      }),
    });
    state.engine = result.engine;
    $("engineBadge").textContent = result.engine === "llm" ? "模型翻译" : "免密钥翻译";
    result.translations.forEach((text, index) => {
      blocks[index].translated = text || "";
      const el = record.transEl.querySelector(`[data-block="${index}"]`);
      if (!el) return;
      el.classList.remove("loading");
      if (text) {
        el.querySelector(".dst").textContent = text;
      } else {
        el.classList.add("failed");
        el.querySelector(".dst").textContent = "这一段的翻译失败了";
      }
    });
    record.transEl.querySelector('[data-role="state"]').textContent = `${blocks.length} 段`;
    record.translated = true;
    cached[record.pageNum] = blocks.map((b) => b.translated);
    store(`trans:${state.sourceLang}:${state.targetLang}`, cached);
    updatePageLabel();
  } catch (error) {
    record.transEl.querySelector('[data-role="state"]').textContent = "翻译失败";
    const first = record.transEl.querySelector(".block.loading");
    if (first) {
      first.classList.remove("loading");
      first.classList.add("failed");
      first.querySelector(".dst").textContent = error.message;
    }
    record.translatePending = false;
    toast("翻译失败：" + error.message, "error");
  }
}

function escapeHtml(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function updatePageLabel() {
  if (!state.pageCount) return;
  const current = Math.min(...(state.visiblePages.size ? state.visiblePages : [1]));
  $("pageLabel").textContent = `${current} / ${state.pageCount}`;
}

/* ---------- 大纲 ---------- */

async function generateOutline() {
  if (!state.pages.length) {
    toast("先打开一份 PDF", "error");
    return;
  }
  const button = $("outlineBtn");
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "生成中…";
  $("outlineState").textContent = "抽取中…";
  const pages = state.pages.map((record) => ({ index: record.pageNum, text: record.text || "" }));
  try {
    const result = await api("/api/reader/outline", {
      method: "POST",
      body: JSON.stringify({ pages, target_lang: state.targetLang }),
    });
    state.outline = result.topics;
    store("outline", state.outline);
    renderOutline();
    $("outlineState").textContent = `${result.topics.length} 个主题 · ${result.model}${result.cached ? " · 缓存" : ""}`;
  } catch (error) {
    $("outlineState").textContent = "失败";
    toast("大纲生成失败：" + error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function renderOutline() {
  const tree = $("outlineTree");
  if (!state.outline || !state.outline.length) {
    tree.innerHTML = '<p class="dim small">没有生成出主题。如果这是扫描件（没有文字层），大纲做不了。</p>';
    return;
  }
  tree.innerHTML = renderTopicList(state.outline, 0);
  for (const node of tree.querySelectorAll("[data-anchor]")) {
    node.addEventListener("click", () => jumpToAnchor(node.dataset.anchor, node));
  }
}

function renderTopicList(topics, depth) {
  return topics
    .map((topic) => {
      const children = renderTopicList(topic.subtopics || [], depth + 1);
      const difficulty = Number(topic.difficulty_score) || 0;
      const level = difficulty >= 70 ? "hard" : difficulty >= 40 ? "mid" : "easy";
      return `<div class="outline-item depth-${depth}">
        <button class="outline-link" data-anchor="${escapeHtml(topic.search_anchor)}" title="${escapeHtml(topic.search_anchor)}">
          <span class="outline-title">${escapeHtml(topic.display_title)}</span>
          <span class="outline-meta"><b class="diff ${level}">${difficulty}</b> · P${topic.page_index || "?"}</span>
        </button>
        ${children ? `<div class="outline-children">${children}</div>` : ""}
      </div>`;
    })
    .join("");
}

function normalizeForSearch(text) {
  return String(text || "").toLowerCase().replace(/\s+/g, " ").trim();
}

function locateAnchor(anchor) {
  // 逐级放宽：全文 → 前 60 字 → 前 30 字。模型偶尔会在锚点尾部带标点差异。
  const needle = normalizeForSearch(anchor);
  if (!needle) return null;
  for (const probe of [needle, needle.slice(0, 60), needle.slice(0, 30)]) {
    if (probe.length < 8) continue;
    for (const record of state.pages) {
      const index = record.blocks.findIndex((block) => normalizeForSearch(block.text).includes(probe));
      if (index >= 0) return { record, index };
    }
  }
  return null;
}

function jumpToAnchor(anchor, node) {
  const hit = locateAnchor(anchor);
  if (!hit) {
    toast("原文里没找到这一段（锚点可能被换行截断）", "error");
    return;
  }
  const reader = $("reader");
  const readerRect = reader.getBoundingClientRect();
  const sectionRect = hit.record.el.getBoundingClientRect();
  // block.y 是按 BASE_SCALE 记的坐标；实际渲染是 BASE_SCALE * zoom
  const offset = hit.record.blocks[hit.index].y * state.zoom;
  reader.scrollTo({
    top: reader.scrollTop + sectionRect.top - readerRect.top + offset - 28,
    behavior: "smooth",
  });
  const blockEl = hit.record.transEl.querySelector(`[data-block="${hit.index}"]`);
  for (const el of [hit.record.paper, blockEl]) {
    if (!el) continue;
    el.classList.remove("anchor-flash");
    void el.offsetWidth;  // 重启动画
    el.classList.add("anchor-flash");
    setTimeout(() => el.classList.remove("anchor-flash"), 2200);
  }
  for (const item of document.querySelectorAll(".outline-link.active")) item.classList.remove("active");
  if (node) node.classList.add("active");
}

/* ---------- 模式切换 ---------- */

function setMode(mode) {
  state.mode = mode;
  for (const button of document.querySelectorAll(".modes .mode")) {
    button.classList.toggle("active", button.dataset.mode === mode);
  }
  document.body.dataset.mode = mode;
  state.pages.forEach((record) => {
    record.ink.style.pointerEvents = mode === "ink" ? "auto" : "none";
    record.paper.style.cursor = mode === "ask" ? "crosshair" : mode === "ink" ? "crosshair" : "default";
  });
  hideAskBox();
}

/* ---------- 术语与词库 ---------- */

async function extractTerms() {
  if (!state.pages.length) {
    toast("先打开一份 PDF", "error");
    return;
  }
  const button = $("termsBtn");
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "抽取中…";
  const pages = state.pages.map((record) => ({ index: record.pageNum, text: record.text || "" }));
  try {
    const result = await api("/api/reader/terms", {
      method: "POST",
      body: JSON.stringify({ pages, target_lang: state.targetLang }),
    });
    state.terms = result.terms;
    renderTerms();
    $("termsState").textContent = `${result.terms.length} 个${result.cached ? " · 缓存" : ""}`;
  } catch (error) {
    $("termsState").textContent = "失败";
    toast("术语抽取失败：" + error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function renderTerms() {
  const tree = $("termsTree");
  if (!state.terms.length) {
    tree.innerHTML = '<p class="dim small">没有抽到术语。扫描件没有文字层，抽不了。</p>';
    return;
  }
  tree.innerHTML = state.terms
    .map((term, index) => {
      const saved = isSaved(term.term);
      return `<div class="term-row" data-term="${escapeHtml(term.term)}">
        <span class="term-word">${escapeHtml(term.term)}</span>
        <span class="term-trans">${escapeHtml(term.translation || "—")}</span>
        ${term.note ? `<span class="term-note">${escapeHtml(term.note)}</span>` : ""}
        <span class="term-page">P${term.page_index || "?"}</span>
        <button class="ghost small" data-save="${index}" ${saved ? "disabled" : ""}>${saved ? "已收藏" : "收藏"}</button>
      </div>`;
    })
    .join("");
  for (const button of tree.querySelectorAll("[data-save]")) {
    button.onclick = () => saveTerm(Number(button.dataset.save));
  }
}

function isSaved(word) {
  return state.vocab.some((item) => item.term.toLowerCase() === String(word).toLowerCase());
}

function saveTerm(index) {
  const term = state.terms[index];
  if (!term || isSaved(term.term)) return;
  state.vocab.push({ ...term });
  store("vocab", state.vocab);
  renderTerms();
  renderVocab();
  toast(`已收藏「${term.term}」`, "ready");
}

function renderVocab() {
  $("vocabCount").textContent = String(state.vocab.length);
  const list = $("vocabList");
  if (!state.vocab.length) {
    list.innerHTML = '<p class="dim small">还没有收藏的术语。</p>';
    return;
  }
  list.innerHTML = state.vocab
    .map(
      (term, index) => `<div class="vocab-row">
        <span class="term-word">${escapeHtml(term.term)}</span>
        <span class="term-trans">${escapeHtml(term.translation || "—")}</span>
        <span class="term-page">P${term.page_index || "?"}</span>
        <button class="ghost small" data-drop="${index}" title="从词库移除">×</button>
      </div>`
    )
    .join("");
  for (const button of list.querySelectorAll("[data-drop]")) {
    button.onclick = () => {
      state.vocab.splice(Number(button.dataset.drop), 1);
      store("vocab", state.vocab);
      renderTerms();
      renderVocab();
    };
  }
}

function exportVocab() {
  if (!state.vocab.length) {
    toast("词库是空的", "error");
    return;
  }
  const lines = ["# 术语词库 · " + state.fileName, ""];
  lines.push("| 术语 | 译名 | 说明 | 页 |");
  lines.push("| --- | --- | --- | --- |");
  for (const term of state.vocab) {
    lines.push(`| ${term.term} | ${term.translation || "—"} | ${term.note || "—"} | ${term.page_index || "?"} |`);
  }
  download(state.fileName + "-术语词库.md", lines.join("\n"));
}

/* ---------- 连续对话与错题本 ---------- */

function chatKey(text) {
  // 同一段文字的圈选视为同一个卡片：用原文指纹做键
  let hash = 2166136261;
  const seed = String(text || "").slice(0, 400);
  for (let i = 0; i < seed.length; i++) {
    hash ^= seed.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function saveChat() {
  const chats = store("chats") || {};
  if (state.chat && state.chat.history.length) {
    chats[state.chat.key] = {
      selection: state.chat.selection,
      pageNum: state.chat.pageNum,
      history: state.chat.history.slice(-20),
      updated: new Date().toISOString(),
    };
  }
  store("chats", chats);
}

function restoreChat(selectionText, pageNum) {
  const chats = store("chats") || {};
  const key = chatKey(selectionText);
  const saved = chats[key];
  if (saved && Array.isArray(saved.history) && saved.history.length) {
    return { key, selection: selectionText, pageNum, history: saved.history };
  }
  return { key, selection: selectionText, pageNum, history: [] };
}

function renderThread() {
  const box = $("askThread");
  const active = Boolean(state.chat && state.chat.history.length);
  $("askNew").classList.toggle("hidden", !active);
  $("askMistake").classList.toggle("hidden", !active);
  if (!active) {
    box.innerHTML = "";
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = state.chat.history
    .map((item) =>
      item.role === "user"
        ? `<div class="bubble me">${escapeHtml(item.content)}</div>`
        : `<div class="bubble tutor">${escapeHtml(item.content)}</div>`
    )
    .join("");
  box.scrollTop = box.scrollHeight;
}

function newChat() {
  state.chat = { key: state.chat?.key || "", selection: state.chat?.selection || "", pageNum: state.chat?.pageNum || 0, history: [] };
  const chats = store("chats") || {};
  if (state.chat.key) delete chats[state.chat.key];
  store("chats", chats);
  renderThread();
  $("askInput").focus();
}

function saveMistake() {
  if (!state.chat || !state.chat.history.length) {
    toast("先问一轮再记", "error");
    return;
  }
  const history = state.chat.history;
  const question = history.filter((item) => item.role === "user").map((item) => item.content).join(" → ") || "（未记录）";
  const answer = [...history].reverse().find((item) => item.role === "assistant")?.content || "";
  const where = `P${state.chat.pageNum} · ${(state.chat.selection || "").slice(0, 30)}`;
  if (state.mistakes.some((item) => item.where === where && item.question === question)) {
    toast("这段已经记过了", "error");
    return;
  }
  state.mistakes.push({
    id: `m${Date.now().toString(36)}`,
    where,
    question,
    answer,
    note: "",
    status: "open",
    createdAt: new Date().toISOString(),
  });
  store("mistakes", state.mistakes);
  renderMistakes();
  toast("已记入错题本", "ready");
}

function renderMistakes() {
  $("mistakeCount").textContent = String(state.mistakes.length);
  const list = $("mistakeList");
  if (!state.mistakes.length) {
    list.innerHTML = '<p class="dim small">还没有错题。提问后点「记为错题」，攒几条就能让模型找共性盲区。</p>';
    return;
  }
  list.innerHTML = state.mistakes
    .map(
      (item, index) => `<div class="mistake-row ${item.status === "done" ? "done" : ""}">
        <button class="mistake-toggle" data-toggle="${index}" title="点击切换掌握状态">${item.status === "done" ? "✓" : "○"}</button>
        <div class="mistake-main">
          <div class="mistake-where">${escapeHtml(item.where)}</div>
          <div class="mistake-q">${escapeHtml(item.question)}</div>
        </div>
        <button class="ghost small" data-drop="${index}" title="删除">×</button>
      </div>`
    )
    .join("");
  for (const button of list.querySelectorAll("[data-toggle]")) {
    button.onclick = () => {
      const item = state.mistakes[Number(button.dataset.toggle)];
      item.status = item.status === "done" ? "open" : "done";
      store("mistakes", state.mistakes);
      renderMistakes();
    };
  }
  for (const button of list.querySelectorAll("[data-drop]")) {
    button.onclick = () => {
      state.mistakes.splice(Number(button.dataset.drop), 1);
      store("mistakes", state.mistakes);
      renderMistakes();
    };
  }
}

async function summarizeMistakes() {
  if (!state.mistakes.length) {
    toast("错题本是空的", "error");
    return;
  }
  const button = $("mistakeSummaryBtn");
  button.disabled = true;
  $("mistakeHint").textContent = "分析中…";
  $("mistakeBody").innerHTML = "";
  try {
    const result = await api("/api/reader/mistakes", {
      method: "POST",
      body: JSON.stringify({ records: state.mistakes }),
    });
    state.mistakeSummary = result;
    renderMistakeSummary();
    $("mistakeModal").classList.remove("hidden");
    $("mistakeHint").textContent = `基于 ${result.count} 条错题 · 模型 ${result.model}`;
  } catch (error) {
    $("mistakeHint").textContent = "分析失败：" + error.message;
    toast("错题总结失败：" + error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function renderMistakeSummary() {
  const summary = state.mistakeSummary || {};
  const blocks = [];
  if (summary.common_blind_spots?.length) {
    blocks.push(`<h4>共性盲区</h4><ul>${summary.common_blind_spots.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`);
  }
  if (summary.review_order?.length) {
    blocks.push(`<h4>复习顺序</h4><ol>${summary.review_order.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol>`);
  }
  if (summary.practice?.length) {
    blocks.push(
      `<h4>练什么</h4>${summary.practice
        .map((item) => `<div class="practice-row"><b>${escapeHtml(item.focus)}</b><span>${escapeHtml(item.how)}</span></div>`)
        .join("")}`
    );
  }
  $("mistakeBody").innerHTML = blocks.join("") || '<p class="dim">没有分析出内容。</p>';
}

function mistakeSummaryMarkdown() {
  const summary = state.mistakeSummary || {};
  const lines = ["# 错题总结 · " + state.fileName, "", `基于 ${summary.count || 0} 条错题`, ""];
  if (summary.common_blind_spots?.length) {
    lines.push("## 共性盲区", "");
    summary.common_blind_spots.forEach((item) => lines.push(`- ${item}`));
    lines.push("");
  }
  if (summary.review_order?.length) {
    lines.push("## 复习顺序", "");
    summary.review_order.forEach((item, index) => lines.push(`${index + 1}. ${item}`));
    lines.push("");
  }
  if (summary.practice?.length) {
    lines.push("## 练什么", "");
    summary.practice.forEach((item) => lines.push(`- **${item.focus}**：${item.how}`));
  }
  return lines.join("\n");
}

function downloadMistakeSummary() {
  if (!state.mistakeSummary) return;
  download(state.fileName + "-错题总结.md", mistakeSummaryMarkdown());
}

function exportMistakes() {
  if (!state.mistakes.length) {
    toast("错题本是空的", "error");
    return;
  }
  const lines = ["# 错题本 · " + state.fileName, ""];
  for (const item of state.mistakes) {
    lines.push(`## ${item.status === "done" ? "✓" : "○"} ${item.where}`, "");
    lines.push(`**问题**：${item.question}`, "");
    if (item.answer) lines.push(`**当时的回答**：${item.answer}`, "");
  }
  download(state.fileName + "-错题本.md", lines.join("\n"));
}

/* ---------- 讲师提问卡 ---------- */

async function makeLecturerCard() {
  if (!state.selection) {
    toast("先在讲义上圈一段", "error");
    return;
  }
  const question = $("askInput").value.trim();
  if (!question) {
    toast("先写一句你卡在哪里", "error");
    return;
  }
  const button = $("askCard");
  button.disabled = true;
  $("cardHint").textContent = "整理中…";
  try {
    const result = await api("/api/reader/lecturer-card", {
      method: "POST",
      body: JSON.stringify({
        selection_text: state.selection.text,
        page_text: state.pages[state.selection.pageNum - 1]?.text || "",
        question,
      }),
    });
    state.card = result.card;
    renderCard();
    $("cardModal").classList.remove("hidden");
    $("cardHint").textContent = "改两个字就能发给老师，或下课直接照着问。";
  } catch (error) {
    $("cardHint").textContent = "整理失败：" + error.message;
    toast("提问卡失败：" + error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function renderCard() {
  const card = state.card || {};
  const labels = {
    module: "模块",
    topic: "知识点",
    current_understanding: "我目前的理解",
    attempt: "我已经试过",
    stuck_point: "我卡在",
    lecturer_question: "想问的问题",
  };
  $("cardBody").innerHTML = Object.entries(labels)
    .map(([key, label]) => `<div class="card-row"><span class="card-label">${label}</span><span class="card-value">${escapeHtml(card[key] || "—")}</span></div>`)
    .join("");
}

function cardMarkdown() {
  const card = state.card || {};
  const labels = {
    module: "模块",
    topic: "知识点",
    current_understanding: "我目前的理解",
    attempt: "我已经试过",
    stuck_point: "我卡在",
    lecturer_question: "想问的问题",
  };
  const lines = ["## 讲师提问卡", ""];
  if (state.fileName) lines.push(`> 讲义：${state.fileName}`, "");
  for (const [key, label] of Object.entries(labels)) {
    lines.push(`**${label}**：${card[key] || "—"}`, "");
  }
  return lines.join("\n");
}

function copyCard() {
  if (!state.card) return;
  navigator.clipboard.writeText(cardMarkdown()).then(
    () => toast("已复制 Markdown", "ready"),
    () => toast("复制失败，手动选一下", "error")
  );
}

function downloadCard() {
  if (!state.card) return;
  download(state.fileName + "-提问卡.md", cardMarkdown());
}

function download(name, text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name.replace(/[\\/:*?"<>|]/g, "-");
  link.click();
  URL.revokeObjectURL(url);
}

/* ---------- 圈选提问 ---------- */

document.addEventListener("pointerdown", (event) => {
  if (state.mode !== "ask") return;
  const paper = event.target.closest(".paper");
  if (!paper || event.target.closest(".pin")) return;
  const record = state.pages.find((item) => item.paper === paper);
  if (!record) return;

  const bounds = paper.getBoundingClientRect();
  const startX = event.clientX - bounds.left;
  const startY = event.clientY - bounds.top;

  const rubber = document.createElement("div");
  rubber.className = "rubber";
  paper.appendChild(rubber);

  const move = (moveEvent) => {
    const x = Math.max(0, Math.min(moveEvent.clientX - bounds.left, bounds.width));
    const y = Math.max(0, Math.min(moveEvent.clientY - bounds.top, bounds.height));
    rubber.style.left = `${Math.min(startX, x)}px`;
    rubber.style.top = `${Math.min(startY, y)}px`;
    rubber.style.width = `${Math.abs(x - startX)}px`;
    rubber.style.height = `${Math.abs(y - startY)}px`;
  };
  const up = (upEvent) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    const x = Math.max(0, Math.min(upEvent.clientX - bounds.left, bounds.width));
    const y = Math.max(0, Math.min(upEvent.clientY - bounds.top, bounds.height));
    const rect = {
      x: Math.min(startX, x),
      y: Math.min(startY, y),
      w: Math.abs(x - startX),
      h: Math.abs(y - startY),
    };
    rubber.remove();
    if (rect.w < 12 || rect.h < 12) return;
    openAskBox(record, rect, bounds);
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up, { once: true });
});

function openAskBox(record, rect, paperBounds) {
  const selectionText = textInRect(record, rect);
  const image = cropSelection(record, rect);
  state.selection = { pageNum: record.pageNum, rect, text: selectionText, image };

  const box = $("askBox");
  $("askQuote").textContent = selectionText || "（这一块没有可提取文字，可能圈的是图或公式）";
  $("askInput").value = "";
  state.chat = restoreChat(selectionText, record.pageNum);
  renderThread();
  box.classList.remove("hidden");

  const top = Math.min(paperBounds.bottom + window.scrollY - 60, window.innerHeight - 260);
  const left = Math.min(paperBounds.right - 340, window.innerWidth - 360);
  box.style.top = `${Math.max(70, top)}px`;
  box.style.left = `${Math.max(12, left)}px`;
  $("askInput").focus();
}

function textInRect(record, rect) {
  // blocks 的 y 是按 BASE_SCALE 的 viewport 记的，rect 是 CSS 像素（= BASE_SCALE × zoom）
  const scale = state.zoom;
  const hits = [];
  for (const block of record.blocks) {
    const blockTop = (block.y - block.height) * scale;
    const blockBottom = (block.lastY + block.height * 1.2) * scale;
    if (blockBottom < rect.y || blockTop > rect.y + rect.h) continue;
    hits.push(block.text);
  }
  if (hits.length) return hits.join("\n");
  // 框太窄时退一步：取垂直方向上最近的一段
  const centerY = (rect.y + rect.h / 2) / scale;
  let nearest = null;
  for (const block of record.blocks) {
    const distance = Math.abs(block.y - centerY);
    if (!nearest || distance < nearest.distance) nearest = { block, distance };
  }
  return nearest ? nearest.block.text : "";
}

function cropSelection(record, rect) {
  const canvas = document.createElement("canvas");
  const ratio = record.canvas.width / record.paper.clientWidth;
  canvas.width = Math.max(1, Math.round(rect.w * ratio));
  canvas.height = Math.max(1, Math.round(rect.h * ratio));
  const context = canvas.getContext("2d");
  context.drawImage(
    record.canvas,
    rect.x * ratio,
    rect.y * ratio,
    canvas.width,
    canvas.height,
    0,
    0,
    canvas.width,
    canvas.height
  );
  return canvas.toDataURL("image/jpeg", 0.82);
}

function hideAskBox() {
  $("askBox").classList.add("hidden");
  state.selection = null;
}

async function submitAsk() {
  if (!state.selection) return;
  const question = $("askInput").value.trim();
  if (!question) {
    toast("先写一句问题", "error");
    return;
  }
  const record = state.pages[state.selection.pageNum - 1];
  const button = $("askSubmit");
  button.disabled = true;
  button.textContent = "思考中…";
  const followUp = Boolean(state.chat && state.chat.history.length);
  try {
    if (followUp) {
      const reply = await api("/api/reader/chat", {
        method: "POST",
        body: JSON.stringify({
          selection_text: state.chat.selection,
          page_text: record.text || "",
          history: state.chat.history,
          message: question,
        }),
      });
      state.chat.history.push({ role: "user", content: question });
      state.chat.history.push({ role: "assistant", content: reply.answer });
      saveChat();
      renderThread();
      $("askInput").value = "";
      return;
    }

    const result = await api("/api/reader/ask", {
      method: "POST",
      body: JSON.stringify({
        question,
        selection_text: state.selection.text,
        page_text: record.blocks.map((b) => b.text).join("\n").slice(0, 6000),
        image: state.selection.image,
      }),
    });
    const pin = {
      id: `p${Date.now().toString(36)}`,
      page: state.selection.pageNum,
      rect: state.selection.rect,
      selection: state.selection.text,
      question,
      answer: result.answer,
      createdAt: new Date().toISOString(),
    };
    state.pins.push(pin);
    store("pins", state.pins);
    drawPins(record);
    renderPinsList();
    state.chat = {
      key: chatKey(state.selection.text),
      selection: state.selection.text,
      pageNum: state.selection.pageNum,
      history: [
        { role: "user", content: question },
        { role: "assistant", content: result.answer },
      ],
    };
    saveChat();
    renderThread();
    $("askInput").value = "";
    $("askInput").focus();
  } catch (error) {
    toast("提问失败：" + error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "问导师";
  }
}

/* ---------- 钉子与侧栏 ---------- */

function drawPins(record) {
  record.pinsEl.innerHTML = "";
  const pins = state.pins.filter((pin) => pin.page === record.pageNum);
  pins.forEach((pin, index) => {
    const marker = document.createElement("button");
    marker.className = "pin";
    marker.textContent = String(index + 1);
    marker.style.left = `${pin.rect.x + pin.rect.w - 10}px`;
    marker.style.top = `${Math.max(0, pin.rect.y - 10)}px`;
    marker.onclick = (event) => {
      event.stopPropagation();
      openPin(pin);
    };
    record.pinsEl.appendChild(marker);
  });
}

function openPin(pin) {
  closePin();
  const record = state.pages[pin.page - 1];
  if (!record) return;
  const pop = document.createElement("div");
  pop.className = "pin-pop";
  pop.innerHTML = `
    <div class="q">${escapeHtml(pin.question)}</div>
    <div class="a">${escapeHtml(pin.answer)}</div>
    <div class="meta"><span>${fmtTime(pin.createdAt)}</span><button class="ghost" data-act="copy">复制</button><button class="ghost" data-act="del">删除</button></div>
  `;
  const roomRight = record.paper.clientWidth - (pin.rect.x + pin.rect.w);
  if (roomRight > 340) {
    pop.style.left = `${pin.rect.x + pin.rect.w + 10}px`;
    pop.style.top = `${Math.max(4, pin.rect.y)}px`;
  } else {
    pop.style.left = `${Math.max(4, Math.min(pin.rect.x, record.paper.clientWidth - 330))}px`;
    pop.style.top = `${pin.rect.y + pin.rect.h + 6}px`;
  }
  pop.querySelector('[data-act="copy"]').onclick = () => navigator.clipboard.writeText(`Q: ${pin.question}\nA: ${pin.answer}`);
  pop.querySelector('[data-act="del"]').onclick = () => {
    state.pins = state.pins.filter((item) => item.id !== pin.id);
    store("pins", state.pins);
    drawPins(record);
    renderPinsList();
    closePin();
  };
  record.paper.appendChild(pop);
  state.pinPop = pop;
}

function closePin() {
  if (state.pinPop) {
    state.pinPop.remove();
    state.pinPop = null;
  }
}

function renderPinsList() {
  const list = $("pins");
  $("pinCount").textContent = String(state.pins.length);
  if (!state.pins.length) {
    list.innerHTML = '<p class="dim small">圈出一段讲义，点「问导师」，回答会钉在原文旁边，也会出现在这里。</p>';
    return;
  }
  list.innerHTML = state.pins
    .map(
      (pin) => `
      <div class="qa" data-id="${pin.id}">
        <div class="q">${escapeHtml(pin.question)}</div>
        <div class="a">${escapeHtml(pin.answer)}</div>
        <div class="meta"><span class="jump" data-act="jump">第 ${pin.page} 页</span><span>${fmtTime(pin.createdAt)}</span></div>
      </div>`
    )
    .join("");
  for (const node of list.querySelectorAll(".qa")) {
    node.querySelector('[data-act="jump"]').onclick = () => {
      const pin = state.pins.find((item) => item.id === node.dataset.id);
      if (!pin) return;
      const record = state.pages[pin.page - 1];
      record.el.scrollIntoView({ behavior: "smooth", block: "start" });
      setTimeout(() => openPin(pin), 400);
    };
  }
}

/* ---------- 手写 ---------- */

function prepareInk(record) {
  const strokes = state.ink[record.pageNum] || [];
  record.strokes = strokes;
  record.ink.style.pointerEvents = state.mode === "ink" ? "auto" : "none";
  record.ink.addEventListener("pointerdown", (event) => {
    if (state.mode !== "ink") return;
    const bounds = record.ink.getBoundingClientRect();
    const scale = record.ink.width / bounds.width;
    state.inkStroke = {
      color: "#1f6feb",
      width: 2.2,
      points: [[(event.clientX - bounds.left) * scale, (event.clientY - bounds.top) * scale]],
    };
    record.strokes.push(state.inkStroke);
    record.ink.setPointerCapture(event.pointerId);
  });
  record.ink.addEventListener("pointermove", (event) => {
    if (!state.inkStroke) return;
    const bounds = record.ink.getBoundingClientRect();
    const scale = record.ink.width / bounds.width;
    state.inkStroke.points.push([(event.clientX - bounds.left) * scale, (event.clientY - bounds.top) * scale]);
    redrawInk(record);
  });
  const stop = () => {
    if (!state.inkStroke) return;
    state.inkStroke = null;
    state.ink[record.pageNum] = record.strokes;
    store("ink", state.ink);
  };
  record.ink.addEventListener("pointerup", stop);
  record.ink.addEventListener("pointercancel", stop);
  record.ink.addEventListener("dblclick", () => {
    record.strokes.length = 0;
    redrawInk(record);
    store("ink", state.ink);
  });
}

function redrawInk(record) {
  const context = record.ink.getContext("2d");
  context.clearRect(0, 0, record.ink.width, record.ink.height);
  context.lineCap = "round";
  context.lineJoin = "round";
  for (const stroke of record.strokes || []) {
    if (stroke.points.length < 2) continue;
    context.strokeStyle = stroke.color || "#1f6feb";
    context.lineWidth = (stroke.width || 2) * (record.ink.width / record.paper.clientWidth || 1);
    context.beginPath();
    context.moveTo(stroke.points[0][0], stroke.points[0][1]);
    for (const point of stroke.points.slice(1)) context.lineTo(point[0], point[1]);
    context.stroke();
  }
}

/* ---------- 导出 ---------- */

function exportMarkdown() {
  if (!state.pdf) return;
  const lines = [`# ${state.fileName}`, "", `> ${state.sourceLang} → ${state.targetLang} · ${state.pageCount} 页`, ""];
  for (const record of state.pages) {
    if (!record.blocks.length) continue;
    lines.push(`## 第 ${record.pageNum} 页`, "");
    record.blocks.forEach((block) => {
      lines.push(`- **原文**：${block.text}`);
      if (block.translated) lines.push(`  **译文**：${block.translated}`);
    });
    lines.push("");
  }
  if (state.pins.length) {
    lines.push("## 提问记录", "");
    for (const pin of state.pins) {
      lines.push(`### 第 ${pin.page} 页 · ${fmtTime(pin.createdAt)}`);
      lines.push(`**Q**：${pin.question}`);
      lines.push(`**A**：${pin.answer}`);
      if (pin.selection) lines.push(`> 圈选原文：${pin.selection.replace(/\n/g, " ")}`);
      lines.push("");
    }
  }
  if (state.notes.trim()) {
    lines.push("## 随手记", "", state.notes.trim(), "");
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${state.fileName}-对照笔记.md`;
  anchor.click();
  URL.revokeObjectURL(url);
}

/* ---------- 事件绑定 ---------- */

function bind() {
  $("outlineBtn").onclick = generateOutline;
  $("termsBtn").onclick = extractTerms;
  $("vocabHide").onchange = () => document.body.classList.toggle("vocab-blur", $("vocabHide").checked);
  $("vocabExport").onclick = exportVocab;
  $("askCard").onclick = makeLecturerCard;
  $("askNew").onclick = newChat;
  $("askMistake").onclick = saveMistake;
  $("mistakeSummaryBtn").onclick = summarizeMistakes;
  $("mistakeExport").onclick = exportMistakes;
  $("mistakeClose").onclick = () => $("mistakeModal").classList.add("hidden");
  $("mistakeDownload").onclick = downloadMistakeSummary;
  $("cardClose").onclick = () => $("cardModal").classList.add("hidden");
  $("cardCopy").onclick = copyCard;
  $("cardDownload").onclick = downloadCard;
  $("fileInput").onchange = (event) => {
    const file = event.target.files?.[0];
    if (file) openFile(file).catch((error) => toast("打开失败：" + error.message, "error"));
  };

  for (const button of document.querySelectorAll(".modes .mode")) {
    button.onclick = () => setMode(button.dataset.mode);
  }

  $("prevBtn").onclick = () => {
    const current = Math.min(...(state.visiblePages.size ? state.visiblePages : [1]));
    const record = state.pages[Math.max(0, current - 2)];
    if (record) record.el.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $("nextBtn").onclick = () => {
    const current = Math.min(...(state.visiblePages.size ? state.visiblePages : [1]));
    const record = state.pages[Math.min(state.pages.length - 1, current)];
    if (record) record.el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  $("zoomIn").onclick = () => {
    state.zoom = Math.min(2.4, state.zoom + 0.15);
    $("zoomLabel").textContent = `${Math.round(state.zoom * 100)}%`;
    state.pages.forEach((record) => {
      record.rendered = false;
      record.ink.width = record.ink.width;  // 保留手写内容，仅重排尺寸
    });
    state.pages.forEach((record) => {
      if (state.visiblePages.has(record.pageNum)) renderCanvas(record);
    });
  };
  $("zoomOut").onclick = () => {
    state.zoom = Math.max(0.6, state.zoom - 0.15);
    $("zoomLabel").textContent = `${Math.round(state.zoom * 100)}%`;
    state.pages.forEach((record) => {
      record.rendered = false;
      if (state.visiblePages.has(record.pageNum)) renderCanvas(record);
    });
  };

  $("askSubmit").onclick = submitAsk;
  $("askCancel").onclick = hideAskBox;
  $("askInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submitAsk();
  });

  $("exportBtn").onclick = exportMarkdown;

  $("noteInput").oninput = () => {
    state.notes = $("noteInput").value;
    store("note", state.notes);
    $("noteState").textContent = "已保存";
    clearTimeout($("noteInput")._timer);
    $("noteInput")._timer = setTimeout(() => ($("noteState").textContent = ""), 1500);
  };

  $("reader").addEventListener("scroll", () => {
    closePin();
  });

  $("sourceLang").onchange = () => {
    state.sourceLang = $("sourceLang").value;
    resetTranslations();
  };
  $("targetLang").onchange = () => {
    state.targetLang = $("targetLang").value;
    resetTranslations();
  };
}

function resetTranslations() {
  state.pages.forEach((record) => {
    record.translated = false;
    record.translatePending = false;
    record.blocks.forEach((block) => (block.translated = ""));
    if (state.visiblePages.has(record.pageNum)) renderTranslation(record);
  });
}

async function boot() {
  bind();
  setMode("read");
  try {
    const health = await api("/api/health");
    $("engineBadge").textContent =
      health.reader.engine === "llm" ? `模型 ${health.reader.llm_model}` : "免密钥翻译";
    $("engineBadge").title = "圈选提问总是用模型；翻译引擎可在启动参数或设置里切换";
  } catch (error) {
    $("engineBadge").textContent = "后端未连接";
  }

  // ?sample=1 直接打开示例讲义（第一次用、或截图时省一步）
  if (new URLSearchParams(location.search).get("sample")) {
    try {
      const response = await fetch("/api/sample");
      if (!response.ok) throw new Error("示例缺失");
      const buffer = await response.arrayBuffer();
      await openBuffer(buffer, "sample.pdf", buffer.byteLength);
    } catch (error) {
      toast("加载示例失败：" + error.message, "error");
    }
  }
}

boot();

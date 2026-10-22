/* aula 上课工作区前端：三栏工作台 + 浏览器端 VAD 采集。
 *
 * 关键设计：断句在前端做。浏览器拿到麦克风后按能量切句，一句完整的话才
 * 发一个二进制帧给后端。这样后端不需要持续接收音频流，也能保证"一句话
 * 一次识别"，译文的上下文是完整的句子而不是半句。
 */

const TARGET_RATE = 16000;
const FRAME_MS = 20; // VAD 判定窗口
const WINDOW_SAMPLES = (TARGET_RATE * FRAME_MS) / 1000;

const state = {
  config: null,
  sessionId: null,
  sessions: [],
  ws: null,
  audio: null, // {ctx, stream, node, source}
  recording: false,
  seq: 0,
  segments: 0,
  latencies: [],
  saveTimer: null,
  dirty: false,
  preview: false,
  startedAt: 0,
};

const $ = (id) => document.getElementById(id);

/* ---------- 工具 ---------- */

function fmtMs(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function fmtDuration(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : `${m}:${String(s % 60).padStart(2, "0")}`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function toast(message, kind = "info") {
  const badge = $("statusBadge");
  badge.textContent = message;
  badge.className = `badge ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    if (!state.recording) setBadge("idle", "待机");
    else setBadge("live", "听课中");
  }, 3600);
}

function setBadge(kind, text) {
  const badge = $("statusBadge");
  badge.textContent = text;
  badge.className = `badge ${kind}`;
}

/* ---------- 断句 Worklet ---------- */

const WORKLET_SRC = `
class VADProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.targetRate = o.targetRate || 16000;
    this.silenceMs = o.silenceMs || 700;
    this.minSpeechMs = o.minSpeechMs || 350;
    this.maxSegmentMs = o.maxSegmentMs || 15000;
    this.ratio = sampleRate / this.targetRate;
    this.windowSamples = (this.targetRate * 20) / 1000;
    this.inBuf = new Float32Array(0);
    this.pos = 0;
    this.win = new Float32Array(0);
    this.clock = 0;
    this.noise = 0.004;
    this.resetSegment();
    this.levelCount = 0;
  }

  resetSegment() {
    this.speaking = false;
    this.chunks = [];
    this.segSamples = 0;
    this.silenceSamples = 0;
    this.startSample = 0;
    this.lastVoice = 0; // 最后一个"有声"窗口的结束位置，用于裁掉尾部静音
  }

  handleWindow(window) {
    let sum = 0;
    for (let i = 0; i < window.length; i++) sum += window[i] * window[i];
    const rms = Math.sqrt(sum / window.length);

    if (!this.speaking) {
      this.noise = this.noise * 0.97 + rms * 0.03;
      const threshold = Math.max(0.010, this.noise * 3);
      if (rms > threshold) {
        this.speaking = true;
        this.chunks = [window];
        this.segSamples = window.length;
        this.silenceSamples = 0;
        this.startSample = this.clock;
        this.lastVoice = window.length;
      }
      this.clock += window.length;
    } else {
      this.chunks.push(window);
      this.segSamples += window.length;
      this.clock += window.length;
      const threshold = Math.max(0.008, this.noise * 2.5);
      if (rms < threshold) {
        this.silenceSamples += window.length;
      } else {
        this.silenceSamples = 0;
        this.lastVoice = this.segSamples;
      }

      const silenceLimit = (this.silenceMs / 1000) * this.targetRate;
      const maxLen = (this.maxSegmentMs / 1000) * this.targetRate;
      if (this.silenceSamples >= silenceLimit || this.segSamples >= maxLen) {
        this.flush();
      }
    }

    this.levelCount += 1;
    if (this.levelCount % 6 === 0) {
      this.port.postMessage({ type: "level", rms });
    }
  }

  flush() {
    if (!this.speaking) return;
    const total = this.segSamples;
    const minSamples = (this.minSpeechMs / 1000) * this.targetRate;

    // 裁掉尾部静音：触发断句的那段静音对识别毫无用处，留着只会让模型
    // 在空白上瞎猜。保留 100ms 余量，避免把最后一个音节的尾音削掉。
    const pad = Math.round(this.targetRate * 0.1);
    let keep = Math.min(total, Math.max(this.lastVoice, 0) + pad);
    if (keep < minSamples) keep = total;
    keep = Math.min(keep, total);

    if (total >= minSamples) {
      const merged = new Float32Array(keep);
      let offset = 0;
      for (const chunk of this.chunks) {
        if (offset >= keep) break;
        const room = Math.min(chunk.length, keep - offset);
        merged.set(chunk.subarray(0, room), offset);
        offset += room;
      }
      const pcm = new Int16Array(keep);
      for (let i = 0; i < keep; i++) {
        const v = Math.max(-1, Math.min(1, merged[i]));
        pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      const startMs = (this.startSample / this.targetRate) * 1000;
      const endMs = ((this.startSample + keep) / this.targetRate) * 1000;
      this.port.postMessage(
        { type: "segment", pcm: pcm.buffer, startMs, endMs },
        [pcm.buffer]
      );
    }
    this.resetSegment();
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0];
    if (!channel) return true;

    const merged = new Float32Array(this.inBuf.length + channel.length);
    merged.set(this.inBuf, 0);
    merged.set(channel, this.inBuf.length);
    this.inBuf = merged;

    const out = [];
    while (this.pos + 1 < this.inBuf.length) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      out.push(this.inBuf[i] * (1 - frac) + this.inBuf[i + 1] * frac);
      this.pos += this.ratio;
    }
    const consumed = Math.floor(this.pos);
    if (consumed > 0) {
      this.inBuf = this.inBuf.slice(consumed);
      this.pos -= consumed;
    }
    if (out.length === 0) return true;

    const resampled = Float32Array.from(out);
    const total = this.win.length + resampled.length;
    const joined = new Float32Array(total);
    joined.set(this.win, 0);
    joined.set(resampled, this.win.length);
    this.win = joined;

    while (this.win.length >= this.windowSamples) {
      const window = this.win.slice(0, this.windowSamples);
      this.win = this.win.slice(this.windowSamples);
      this.handleWindow(window);
    }
    return true;
  }
}
registerProcessor("vad", VADProcessor);
`;

/* ---------- 录音链路 ---------- */

async function startRecording() {
  if (state.recording) return;
  if (!state.sessionId) {
    toast("先新建一节课", "error");
    return;
  }
  if (!window.AudioWorkletNode) {
    toast("浏览器不支持 AudioWorklet，请用新版 Safari 或 Chrome", "error");
    return;
  }

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (err) {
    toast("麦克风权限被拒绝：" + err.message, "error");
    return;
  }

  const ctx = new AudioContext({ sampleRate: TARGET_RATE });
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" })));

  const source = ctx.createMediaStreamSource(stream);
  // outputs 必须是 1，并且接到一个静音节点：否则浏览器可能认为这个节点
  // 没有下游、不驱动它，process() 就不会被调用（静音采集的经典坑）。
  const node = new AudioWorkletNode(ctx, "vad", {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
    processorOptions: {
      targetRate: TARGET_RATE,
      silenceMs: state.config.silence_ms,
      minSpeechMs: state.config.min_speech_ms,
      maxSegmentMs: state.config.max_segment_ms,
    },
  });

  node.port.onmessage = (event) => {
    const data = event.data;
    if (data.type === "level") {
      const pct = Math.min(100, Math.round((Math.sqrt(data.rms) * 900)));
      $("levelBar").style.width = pct + "%";
      return;
    }
    if (data.type === "segment") sendSegment(data);
  };

  const sink = ctx.createGain();
  sink.gain.value = 0;
  source.connect(node);
  node.connect(sink);
  sink.connect(ctx.destination);
  state.audio = { ctx, stream, node, source, sink };
  state.recording = true;
  state.startedAt = Date.now();
  $("recordBtn").textContent = "结束听课";
  $("recordBtn").classList.add("running");
  setBadge("live", "听课中");
  $("emptyHint").classList.add("hidden");
  connectWs();
  updateStatus();
}

async function stopRecording() {
  if (!state.recording) return;
  state.recording = false;
  $("recordBtn").textContent = "开始听课";
  $("recordBtn").classList.remove("running");
  $("levelBar").style.width = "0%";

  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "flush" }));
  }
  setTimeout(() => {
    if (state.ws) {
      state.ws.close();
      state.ws = null;
    }
  }, 600);

  if (state.audio) {
    try {
      state.audio.node.port.postMessage({ type: "stop" });
      state.audio.source.disconnect();
      state.audio.node.disconnect();
      if (state.audio.sink) state.audio.sink.disconnect();
      state.audio.stream.getTracks().forEach((track) => track.stop());
      await state.audio.ctx.close();
    } catch (_) {}
    state.audio = null;
  }
  setBadge("idle", "已结束");
  updateStatus();
}

function sendSegment({ pcm, startMs, endMs }) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    // 以前这里直接 return，等于"这节课记全了"变成一句没人验证的话。
    // 重连期间的句子是真的丢了——说出来，只提示一次，别刷屏。
    if (state.recording && !sendSegment._warned) {
      sendSegment._warned = true;
      toast("实时连接未就绪，有句子没记上（正在重连）", "error");
    }
    return;
  }
  sendSegment._warned = false;
  const bytes = new Uint8Array(pcm);
  const frame = new Uint8Array(8 + bytes.length);
  const view = new DataView(frame.buffer);
  view.setUint32(0, Math.round(startMs), true);
  view.setUint32(4, Math.round(endMs), true);
  frame.set(bytes, 8);
  state.ws.send(frame);
}

function setWs(state, text) {
  const dot = $("wsDot");
  if (!dot) return;
  dot.className = "ws-dot " + state;
  dot.textContent = text;
}

function connectWs() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/live?session_id=${state.sessionId}`);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    setBadge("ready", "已连接");
    setWs("on", "已连接");
  };
  ws.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (data.type === "ready") {
      state.seq = data.last_seq || 0;
      $("statProvider").textContent = `转写 ${data.asr.provider}`;
      if (data.asr.provider === "mock") toast("演示模式：转写是预置句子", "ready");
      return;
    }
    if (data.type === "transcript") {
      state.seq = data.utterance.seq;
      state.segments += 1;
      if (data.latency_ms) {
        state.latencies.push(data.latency_ms);
        if (state.latencies.length > 20) state.latencies.shift();
      }
      appendUtterance(data.utterance);
      updateStatus();
      return;
    }
    if (data.type === "translation") {
      // 原文先到、译文后到：把译文填回对应那句（服务端为此单独推一条）
      applyTranslation(data.seq, data.target);
      return;
    }
    if (data.type === "summary") {
      renderSummary(data.summary);
      toast("已自动生成总结", "ready");
      return;
    }
    if (data.type === "status") {
      toast(data.message, "ready");
      return;
    }
    if (data.type === "error") {
      toast(data.message, "error");
      console.warn("[aula]", data);
      return;
    }
  };
  ws.onclose = () => {
    setWs("off", "连接断开");
    if (state.recording) setTimeout(connectWs, 1200);
  };
  ws.onerror = () => {};
  state.ws = ws;
}

/* ---------- 转写渲染 ---------- */

function appendUtterance(item) {
  $("emptyHint").classList.add("hidden");
  const node = document.createElement("div");
  node.className = "utt";
  node.dataset.seq = item.seq;

  const row = document.createElement("div");
  row.className = "row";

  const time = document.createElement("span");
  time.className = "time";
  time.textContent = fmtMs(item.start_ms);

  const text = document.createElement("div");
  text.className = "text";

  const src = document.createElement("div");
  src.className = "src";
  src.textContent = item.source || "";
  text.appendChild(src);

  if (item.target) {
    const dst = document.createElement("div");
    dst.className = "dst";
    dst.textContent = item.target;
    text.appendChild(dst);
  } else if (state.config && state.config.translate !== false) {
    // 译文还没到：先占位，让"正在翻"这件事看得见，而不是像漏了没翻
    const pending = document.createElement("div");
    pending.className = "dst pending";
    text.appendChild(pending);
  }

  row.appendChild(time);
  row.appendChild(text);
  node.appendChild(row);

  const actions = document.createElement("div");
  actions.className = "actions";
  const insertBtn = document.createElement("button");
  insertBtn.className = "ghost";
  insertBtn.textContent = "插入";
  insertBtn.onclick = () => insertToNote(item);
  const copyBtn = document.createElement("button");
  copyBtn.className = "ghost";
  copyBtn.textContent = "复制";
  copyBtn.onclick = () => navigator.clipboard.writeText(`${item.source}\n${item.target || ""}`);
  actions.appendChild(insertBtn);
  actions.appendChild(copyBtn);
  node.appendChild(actions);

  $("transcript").appendChild(node);
  if ($("autoScroll").checked) {
    $("transcript").scrollTop = $("transcript").scrollHeight;
  }
}

/** 译文后到：填回对应那句。找不到节点就说明这节课已经被切走了，静默忽略。 */
function applyTranslation(seq, target) {
  if (!target) return;
  const node = $("transcript").querySelector(`.utt[data-seq="${seq}"]`);
  if (!node) return;
  const text = node.querySelector(".text");
  if (!text) return;
  const dst = text.querySelector(".dst");
  if (dst) {
    dst.textContent = target;
    dst.classList.remove("pending");
  } else {
    const fresh = document.createElement("div");
    fresh.className = "dst";
    fresh.textContent = target;
    text.appendChild(fresh);
  }
}

function insertToNote(item) {
  const input = $("noteInput");
  const quote = `> [${fmtMs(item.start_ms)}] ${item.source}\n> ${item.target || ""}\n\n`;
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  input.setRangeText(quote, start, end, "end");
  input.focus();
  scheduleSave();
}

/* ---------- 笔记 ---------- */

function scheduleSave() {
  state.dirty = true;
  $("saveState").textContent = "输入中…";
  $("saveState").className = "meta saving";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveNote, 800);
}

async function saveNote() {
  if (!state.sessionId || !state.dirty) return;
  state.dirty = false;
  try {
    await api(`/api/sessions/${state.sessionId}/note`, {
      method: "PUT",
      body: JSON.stringify({ content: $("noteInput").value }),
    });
    const time = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    $("saveState").textContent = `已保存 ${time}`;
    $("saveState").className = "meta saved";
  } catch (err) {
    $("saveState").textContent = "保存失败";
    $("saveState").className = "meta";
    console.error(err);
  }
}

function renderMiniMarkdown(text) {
  const escape = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines = escape(text).split("\n");
  const out = [];
  let inList = false;
  let inQuote = false;
  let table = [];

  // 听课记录里有关键术语表，纯 pre 显示会变成一堆竖线，所以补一个极简表格解析
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((cells) => !cells.every((cell) => /^:?-{2,}:?$/.test(cell.trim())));
    if (rows.length) {
      const head = rows[0].map((cell) => `<th>${inline(cell.trim())}</th>`).join("");
      const body = rows
        .slice(1)
        .map((cells) => `<tr>${cells.map((cell) => `<td>${inline(cell.trim())}</td>`).join("")}</tr>`)
        .join("");
      out.push(`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`);
    }
    table = [];
  };

  const closeBlocks = () => {
    flushTable();
    if (inList) {
      out.push("</ul>");
      inList = false;
    }
    if (inQuote) {
      out.push("</blockquote>");
      inQuote = false;
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^\s*$/.test(line)) {
      closeBlocks();
      continue;
    }
    if (/^\s*\|/.test(line)) {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      if (inQuote) {
        out.push("</blockquote>");
        inQuote = false;
      }
      table.push(line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|"));
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      flushTable();
    }
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      closeBlocks();
      out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`);
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      if (inQuote) {
        out.push("</blockquote>");
        inQuote = false;
      }
      if (!inList) {
        out.push("<ul>");
        inList = true;
      }
      out.push(`<li>${inline(line.replace(/^[-*]\s+/, ""))}</li>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      if (!inQuote) {
        out.push("<blockquote>");
        inQuote = true;
      }
      out.push(`<div>${inline(line.replace(/^>\s?/, ""))}</div>`);
      continue;
    }
    closeBlocks();
    out.push(`<p>${inline(line)}</p>`);
  }
  closeBlocks();
  return out.join("\n");
}

function inline(text) {
  return text
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function togglePreview() {
  state.preview = !state.preview;
  const textarea = $("noteInput");
  const preview = $("notePreview");
  if (state.preview) {
    preview.innerHTML = renderMiniMarkdown(textarea.value) || '<p class="placeholder">还没有笔记。</p>';
    preview.classList.remove("hidden");
    textarea.classList.add("hidden");
    $("previewBtn").textContent = "编辑";
  } else {
    preview.classList.add("hidden");
    textarea.classList.remove("hidden");
    $("previewBtn").textContent = "预览";
  }
}

/* ---------- 总结 ---------- */

async function makeSummary() {
  if (!state.sessionId) return;
  const button = $("summarizeBtn");
  button.disabled = true;
  button.textContent = "生成中…";
  try {
    if (state.dirty) await saveNote();
    const record = await api(`/api/sessions/${state.sessionId}/summary`, {
      method: "POST",
      body: JSON.stringify({ kind: "full" }),
    });
    renderSummary(record);
  } catch (err) {
    toast("总结失败：" + err.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "一键总结";
  }
}

function renderSummary(record) {
  const container = $("summary");
  if (!record || !record.content) {
    container.innerHTML = '<p class="placeholder">还没有总结。听完一节课点「一键总结」。</p>';
    return;
  }
  const content = record.content;
  const parts = [];

  if (content.overview) parts.push(`<div class="overview">${inline(content.overview)}</div>`);

  if (content.points && content.points.length) {
    parts.push("<h3>要点提炼</h3><ol>");
    for (const point of content.points) {
      const heading = point.heading ? `<strong>${inline(point.heading)}</strong>` : "";
      const detail = point.detail && point.detail !== point.heading ? `　${inline(point.detail)}` : "";
      parts.push(`<li>${heading}${detail}</li>`);
    }
    parts.push("</ol>");
  }

  if (content.terms && content.terms.length) {
    parts.push("<h3>关键术语</h3><table><thead><tr><th>原文</th><th>中文</th><th>说明</th></tr></thead><tbody>");
    for (const term of content.terms) {
      parts.push(
        `<tr><td>${inline(term.term || "")}</td><td>${inline(term.translation || "")}</td><td>${inline(term.note || "")}</td></tr>`
      );
    }
    parts.push("</tbody></table>");
  }

  if (content.emphasis && content.emphasis.length) {
    parts.push("<h3>老师强调 / 考点</h3><ul>");
    for (const item of content.emphasis) parts.push(`<li>${inline(item)}</li>`);
    parts.push("</ul>");
  }

  if (content.todos && content.todos.length) {
    parts.push("<h3>待办</h3><ul>");
    for (const item of content.todos) parts.push(`<li>☐ ${inline(item)}</li>`);
    parts.push("</ul>");
  }

  if (content.questions && content.questions.length) {
    parts.push("<h3>待查问题</h3><ul>");
    for (const item of content.questions) parts.push(`<li>${inline(item)}</li>`);
    parts.push("</ul>");
  }

  parts.push(
    `<div class="meta">${record.created_at ? record.created_at.replace("T", " ").slice(0, 16) : ""} · 模型 ${record.model || "—"} · 覆盖 ${record.covers_upto || 0} 句</div>`
  );
  container.innerHTML = parts.join("\n");
  $("summaryMeta").textContent = record.kind === "auto" ? "自动总结" : "已生成";
}

/* ---------- 会话 ---------- */

async function loadSessions(selectId) {
  state.sessions = await api("/api/sessions");
  const select = $("sessionSelect");
  select.innerHTML = "";
  for (const session of state.sessions) {
    const option = document.createElement("option");
    option.value = session.id;
    const stamp = (session.created_at || "").replace("T", " ").slice(5, 16);
    option.textContent = `${session.title}（${stamp}）`;
    select.appendChild(option);
  }
  if (!state.sessions.length) {
    const session = await api("/api/sessions", { method: "POST", body: JSON.stringify({}) });
    state.sessions = [session];
    return loadSessions(session.id);
  }
  state.sessionId = selectId && state.sessions.some((s) => s.id === selectId) ? selectId : state.sessions[0].id;
  select.value = state.sessionId;
}

async function loadSession(sessionId) {
  const detail = await api(`/api/sessions/${sessionId}`);
  state.sessionId = sessionId;
  $("noteInput").value = detail.note || "";
  state.seq = detail.utterances.length ? detail.utterances[detail.utterances.length - 1].seq : 0;
  state.segments = detail.utterances.length;
  state.latencies = detail.utterances.map((u) => u.latency_ms).filter((v) => typeof v === "number" && v > 0).slice(-20);

  const transcript = $("transcript");
  transcript.innerHTML = "";
  for (const item of detail.utterances) appendUtterance(item);
  $("emptyHint").classList.toggle("hidden", detail.utterances.length > 0);

  renderSummary(detail.summary);
  $("statSession").textContent = detail.session.title;
  $("statSegments").textContent = `${detail.utterances.length} 句`;
  updateStatus();

  // 兜底：新建/切换课堂在界面上都已在录音时拦住。真走到这里说明有调用绕过了
  // 守卫——先把当前这节课收尾（flush 进旧 session），绝不让音频漂移到另一节课名下。
  if (state.recording) await stopRecording();
}

/** 录音期间不许换课堂：换 session 等于把这节课的后半段记到另一节课名下。 */
function blockWhileRecording(action) {
  if (!state.recording) return false;
  toast(`正在听课，先点「结束听课」再${action}`, "error");
  return true;
}

async function newSession() {
  if (blockWhileRecording("新建课堂")) return;
  const title = prompt("这节课叫什么？", new Date().toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) + " 的课");
  if (title === null) return;
  const session = await api("/api/sessions", { method: "POST", body: JSON.stringify({ title }) });
  await loadSessions(session.id);
  await loadSession(session.id);
}

async function makeDigest() {
  if (!state.sessionId) {
    toast("先新建一节课", "error");
    return;
  }
  const button = $("digestBtn");
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "生成中…";
  $("digestHint").textContent = "正在分块抽取再合成，长课可能需要十几秒。";
  try {
    const result = await api(`/api/sessions/${state.sessionId}/digest`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    state.digest = result.markdown;
    // 展示时去掉 YAML frontmatter，渲染成 HTML；下载仍用原始 Markdown
    $("digestBody").innerHTML = renderMiniMarkdown(
      result.markdown.replace(/^---\n[\s\S]*?\n---\n/, "")
    );
    $("digestHint").textContent = `模型 ${result.model} · ${result.markdown.length} 字符 · 可直接放进 Obsidian`;
    $("digestModal").classList.remove("hidden");
  } catch (error) {
    toast("生成失败：" + error.message, "error");
    $("digestHint").textContent = "生成失败：" + error.message;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function deleteSession() {
  if (!state.sessionId) return;
  if (state.recording) {
    toast("先结束听课再删除", "error");
    return;
  }
  const session = state.sessions.find((s) => s.id === state.sessionId);
  const label = session ? session.title : "这节课";
  if (!confirm(`删除「${label}」？转写、笔记、总结会一起删掉，无法恢复。`)) return;
  await api(`/api/sessions/${state.sessionId}`, { method: "DELETE" });
  state.sessionId = null;
  await loadSessions();
  await loadSession(state.sessionId);
  toast("已删除", "ready");
}

/* ---------- 状态与设置 ---------- */

function updateStatus() {
  $("statSegments").textContent = `${state.segments} 句`;
  $("transcriptMeta").textContent = state.recording
    ? `听课中 ${fmtDuration(Date.now() - state.startedAt)}`
    : `${state.segments} 句`;
  if (state.latencies.length) {
    const avg = Math.round(state.latencies.reduce((a, b) => a + b, 0) / state.latencies.length);
    $("statLatency").textContent = `延迟 ${avg}ms`;
  }
}

setInterval(() => {
  if (state.recording) updateStatus();
}, 1000);

function fillLanguageSelects(select, languages, value) {
  select.innerHTML = "";
  for (const [code, name] of Object.entries(languages || {})) {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = `${name}（${code}）`;
    if (code === value) option.selected = true;
    select.appendChild(option);
  }
}

async function loadConfig() {
  state.config = await api("/api/config");
  const cfg = state.config;
  fillLanguageSelects($("sourceLang"), cfg.languages, cfg.source_lang);
  fillLanguageSelects($("targetLang"), cfg.languages, cfg.target_lang);
  fillLanguageSelects($("cfgSourceLang"), cfg.languages, cfg.source_lang);
  fillLanguageSelects($("cfgTargetLang"), cfg.languages, cfg.target_lang);

  const providerSelect = $("cfgAsrProvider");
  providerSelect.innerHTML = "";
  for (const [code, name] of Object.entries(cfg.asr_providers || {})) {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = name;
    if (code === cfg.asr_provider) option.selected = true;
    providerSelect.appendChild(option);
  }

  const presetSelect = $("cfgProvider");
  presetSelect.innerHTML = "";
  const noneOption = document.createElement("option");
  noneOption.value = "";
  noneOption.textContent = "自定义（手填下面的地址与模型）";
  presetSelect.appendChild(noneOption);
  for (const item of cfg.providers || []) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.label}（${item.model}）`;
    if (item.id === cfg.provider) option.selected = true;
    presetSelect.appendChild(option);
  }
  state.providers = cfg.providers || [];
  state.mtProviders = cfg.mt_providers || [];
  updateProviderHint();

  const engineSelect = $("cfgTranslateEngine");
  const llmOption = document.createElement("option");
  llmOption.value = "llm";
  llmOption.textContent = "大模型翻译（默认，术语更准）";
  engineSelect.appendChild(llmOption);
  for (const item of cfg.mt_providers || []) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.key === "需要" ? item.label : `${item.label}`;
    engineSelect.appendChild(option);
  }
  engineSelect.value = cfg.translate_engine || "llm";
  $("cfgMtApiKey").value = "";
  $("cfgMtApiKey").placeholder = cfg.mt_api_key ? `已配置（${cfg.mt_api_key}）` : "选了专用接口才需要";
  $("cfgMtRegion").value = cfg.mt_region || "";
  updateMtHint();

  $("cfgTranslate").checked = !!cfg.translate;
  $("cfgAsrModel").value = cfg.asr_model || "";
  $("cfgAsrBaseUrl").value = cfg.asr_base_url || "";
  $("cfgAsrApiKey").value = "";
  $("cfgAsrApiKey").placeholder = cfg.asr_api_key ? `已配置（${cfg.asr_api_key}）` : "sk-...";
  $("cfgLlmBaseUrl").value = cfg.llm_base_url || "";
  $("cfgLlmModel").value = cfg.llm_model || "";
  $("cfgLlmEffort").value = cfg.llm_reasoning_effort || "";
  $("cfgLlmApiKey").value = "";
  $("cfgLlmApiKey").placeholder = cfg.llm_api_key ? `已配置（${cfg.llm_api_key}）` : "";
  $("cfgSilenceMs").value = cfg.silence_ms;
  $("cfgMinSpeechMs").value = cfg.min_speech_ms;
  $("cfgMaxSegmentMs").value = cfg.max_segment_ms;
  $("cfgAutoSummary").value = cfg.auto_summary_minutes;
  $("cfgResolved").textContent =
    `当前生效：转写 ${cfg.resolved.asr_provider} / ${cfg.resolved.asr_model} · 翻译 ${cfg.resolved.translate_model} · 总结 ${cfg.resolved.summary_model}` +
    (cfg.llm_reasoning_effort ? ` · 思考档 ${cfg.llm_reasoning_effort}` : "");
  $("statProvider").textContent = `转写 ${cfg.resolved.asr_provider}`;
}

function currentPreset() {
  const id = $("cfgProvider").value;
  return (state.providers || []).find((item) => item.id === id) || null;
}

function updateProviderHint() {
  const preset = currentPreset();
  const hint = $("cfgProviderHint");
  if (!preset) {
    hint.textContent = "自定义：自己填接口地址与模型，任何 OpenAI 兼容服务都行。";
    return;
  }
  hint.textContent = `${preset.limits} · 上下文 ${preset.context} · 密钥${preset.key}${preset.key_env ? "（" + preset.key_env + "）" : ""}${preset.free ? " · " + preset.free : ""}`;
}

function updateMtHint() {
  const engine = $("cfgTranslateEngine").value;
  const hint = $("cfgMtHint");
  if (engine === "llm") {
    hint.textContent = "当前用大模型翻译（跟着上面的服务商走）。优点：能给术语注原文、断句也译得通；缺点：一次 0.6–2 秒，高峰期可能 503。";
    return;
  }
  const info = (state.mtProviders || []).find((item) => item.id === engine);
  if (!info) {
    hint.textContent = "";
    return;
  }
  const keyLine = info.key_env
    ? `密钥变量 ${info.key_env}${info.needs_region ? "（还要填地区）" : ""}`
    : "无需密钥，直接可用";
  hint.textContent = `${info.limits} · ${info.free} · ${keyLine}`;
}

function applyPresetToForm() {
  const preset = currentPreset();
  updateProviderHint();
  if (!preset) return;
  $("cfgLlmBaseUrl").value = preset.base_url;
  $("cfgLlmModel").value = preset.model;
  if (preset.asr_mode) {
    $("cfgAsrProvider").value = preset.asr_mode === "gemini" ? "gemini" : "openai";
  }
  $("cfgAsrBaseUrl").value = preset.base_url;
}

async function saveSettings() {
  const payload = {
    provider: $("cfgProvider").value,
    source_lang: $("cfgSourceLang").value,
    target_lang: $("cfgTargetLang").value,
    translate: $("cfgTranslate").checked,
    translate_engine: $("cfgTranslateEngine").value,
    mt_region: $("cfgMtRegion").value.trim(),
    asr_provider: $("cfgAsrProvider").value,
    asr_model: $("cfgAsrModel").value.trim(),
    asr_base_url: $("cfgAsrBaseUrl").value.trim(),
    llm_base_url: $("cfgLlmBaseUrl").value.trim(),
    llm_model: $("cfgLlmModel").value.trim(),
    llm_reasoning_effort: $("cfgLlmEffort").value,
    silence_ms: Number($("cfgSilenceMs").value) || 700,
    min_speech_ms: Number($("cfgMinSpeechMs").value) || 350,
    max_segment_ms: Number($("cfgMaxSegmentMs").value) || 15000,
    auto_summary_minutes: Number($("cfgAutoSummary").value) || 0,
  };
  if ($("cfgAsrApiKey").value.trim()) payload.asr_api_key = $("cfgAsrApiKey").value.trim();
  if ($("cfgLlmApiKey").value.trim()) payload.llm_api_key = $("cfgLlmApiKey").value.trim();
  if ($("cfgMtApiKey").value.trim()) payload.mt_api_key = $("cfgMtApiKey").value.trim();

  try {
    state.config = await api("/api/config", { method: "PUT", body: JSON.stringify(payload) });
    fillLanguageSelects($("sourceLang"), state.config.languages, state.config.source_lang);
    fillLanguageSelects($("targetLang"), state.config.languages, state.config.target_lang);
    $("settingsModal").classList.add("hidden");
    toast("设置已生效", "ready");
    if (state.recording) {
      toast("当前录音使用的是旧参数，重新开始听课后生效", "ready");
    }
  } catch (err) {
    toast("保存失败：" + err.message, "error");
  }
}

/* ---------- 启动 ---------- */

async function boot() {
  $("recordBtn").onclick = () => (state.recording ? stopRecording() : startRecording());
  $("newSessionBtn").onclick = newSession;
  $("deleteSessionBtn").onclick = deleteSession;
  $("settingsBtn").onclick = () => $("settingsModal").classList.remove("hidden");
  $("closeSettings").onclick = () => $("settingsModal").classList.add("hidden");
  $("saveSettings").onclick = saveSettings;
  $("cfgProvider").onchange = applyPresetToForm;
  $("cfgTranslateEngine").onchange = updateMtHint;
  $("summarizeBtn").onclick = makeSummary;
  $("previewBtn").onclick = togglePreview;
  $("noteInput").oninput = () => {
    scheduleSave();
    if (state.preview) togglePreview();
  };
  $("sessionSelect").onchange = async (event) => {
    const next = event.target.value;
    // 拦住并把下拉框弹回去：否则界面显示的是新课堂、内容却是旧课堂的
    if (blockWhileRecording("切换课堂")) {
      event.target.value = state.sessionId;
      return;
    }
    if (state.dirty) await saveNote();
    await loadSession(next);
  };
  $("sourceLang").onchange = async (event) => {
    await api("/api/config", { method: "PUT", body: JSON.stringify({ source_lang: event.target.value }) });
    await loadConfig();
    toast("课堂语言已切换", "ready");
  };
  $("targetLang").onchange = async (event) => {
    await api("/api/config", { method: "PUT", body: JSON.stringify({ target_lang: event.target.value }) });
    await loadConfig();
    toast("笔记语言已切换", "ready");
  };
  $("exportBtn").onclick = () => {
    if (!state.sessionId) return;
    window.open(`/api/sessions/${state.sessionId}/export?transcript=1`, "_blank");
  };

  $("digestBtn").onclick = makeDigest;
  $("closeDigest").onclick = () => $("digestModal").classList.add("hidden");
  $("downloadDigest").onclick = () => {
    if (!state.digest) return;
    const blob = new Blob([state.digest], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const title = ($("sessionSelect").selectedOptions[0]?.textContent || "aula").replace(/[\\/:*?"<>|]/g, "-");
    link.href = url;
    link.download = `${title}.md`;
    link.click();
    URL.revokeObjectURL(url);
  };

  await loadConfig();
  await loadSessions();
  await loadSession(state.sessionId);
  if (location.hash === "#settings") $("settingsModal").classList.remove("hidden");
}

boot().catch((err) => {
  console.error(err);
  toast("初始化失败：" + err.message, "error");
});

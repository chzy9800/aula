/* VAD Worklet 的行为测试（Node 环境，不依赖浏览器）。
 *
 * 断句逻辑是整个工具最容易出错的地方：静音阈值、重采样、强制切句任何一处
 * 写错，表现都是"录音没反应"或"一句话被切成三段"，而在浏览器里很难调试。
 * 所以这里把 worklet 的源码抽出来，在 Node 里用假音频喂它，直接验证输出。
 *
 * 运行：node tests/vad.test.mjs
 */

import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";

const appJs = fs.readFileSync(new URL("../aula/static/app.js", import.meta.url), "utf8");
const match = appJs.match(/const WORKLET_SRC = `([\s\S]*?)`;/);
assert.ok(match, "未能从 app.js 中提取 WORKLET_SRC");

const workletSrc = match[1];

// —— 伪造 AudioWorklet 运行环境 ——
let messages = [];
globalThis.sampleRate = 48000;
globalThis.AudioWorkletProcessor = class {
  constructor() {
    this.port = { postMessage: (msg) => messages.push(msg) };
  }
};
let ProcessorClass = null;
globalThis.registerProcessor = (name, cls) => {
  ProcessorClass = cls;
};

new Function(workletSrc)();
assert.ok(ProcessorClass, "registerProcessor 未被调用");

function makeProcessor(overrides = {}) {
  messages = [];
  return new ProcessorClass({
    processorOptions: {
      targetRate: 16000,
      silenceMs: 300,
      minSpeechMs: 200,
      maxSegmentMs: 3000,
      ...overrides,
    },
  });
}

const BLOCK = 128; // AudioWorklet 的固定块长
const CTX_RATE = 48000;

function block(kind, amplitude = 0.3) {
  const data = new Float32Array(BLOCK);
  if (kind === "tone") {
    for (let i = 0; i < BLOCK; i++) data[i] = amplitude * Math.sin((2 * Math.PI * 300 * i) / CTX_RATE);
  }
  return data;
}

function feed(processor, seconds, kind, amplitude) {
  const blocks = Math.ceil((seconds * CTX_RATE) / BLOCK);
  for (let i = 0; i < blocks; i++) processor.process([[block(kind, amplitude)]], []);
}

function segments() {
  return messages.filter((m) => m.type === "segment");
}

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`  ok  ${name}`);
}

console.log("VAD worklet");

test("纯静音不产生片段", () => {
  const processor = makeProcessor();
  feed(processor, 2, "silence");
  assert.equal(segments().length, 0);
});

test("说话后静音会切出一段", () => {
  const processor = makeProcessor();
  feed(processor, 1.0, "tone");
  feed(processor, 1.0, "silence");
  const segs = segments();
  assert.equal(segs.length, 1, `应切出 1 段，实际 ${segs.length}`);
  const seg = segs[0];
  assert.ok(seg.pcm instanceof ArrayBuffer, "pcm 应是 ArrayBuffer");
  const samples = seg.pcm.byteLength / 2;
  assert.ok(samples > 8000, `语音长度应接近 1 秒，实际 ${samples} 采样`);
  assert.ok(seg.endMs > seg.startMs, "结束时间必须大于开始时间");
  assert.ok(Math.abs(seg.startMs) < 150, `起点应接近 0，实际 ${seg.startMs}`);
});

test("输出采样率被重采样到 16k", () => {
  const processor = makeProcessor();
  feed(processor, 1.0, "tone");
  feed(processor, 1.0, "silence");
  const samples = segments()[0].pcm.byteLength / 2;
  // 1 秒语音 → 约 16000 采样；允许 20% 误差（VAD 起点判定 + 尾部静音）
  assert.ok(samples > 12000 && samples < 19000, `重采样后长度异常：${samples}`);
});

test("过短的声音被丢弃", () => {
  const processor = makeProcessor({ minSpeechMs: 500 });
  feed(processor, 0.1, "tone");
  feed(processor, 1.0, "silence");
  assert.equal(segments().length, 0, "100ms 的声音应低于 500ms 阈值被丢弃");
});

test("超长语音被强制切分", () => {
  const processor = makeProcessor({ maxSegmentMs: 1000 });
  feed(processor, 3.0, "tone");
  const segs = segments();
  assert.ok(segs.length >= 2, `3 秒语音在 1 秒上限下应切成多段，实际 ${segs.length}`);
  for (const seg of segs) {
    const samples = seg.pcm.byteLength / 2;
    assert.ok(samples <= 16000 * 1.15, `单段不应超过上限太多：${samples}`);
  }
});

test("会推送音量电平", () => {
  const processor = makeProcessor();
  feed(processor, 1.0, "tone");
  const levels = messages.filter((m) => m.type === "level");
  assert.ok(levels.length > 0, "应上报音量用于界面电平条");
  assert.ok(levels.every((m) => typeof m.rms === "number" && m.rms >= 0));
});

test("多段语音按顺序输出且时间递增", () => {
  const processor = makeProcessor({ silenceMs: 200 });
  for (let round = 0; round < 3; round++) {
    feed(processor, 0.6, "tone");
    feed(processor, 0.8, "silence");
  }
  const segs = segments();
  assert.equal(segs.length, 3, `应得到 3 段，实际 ${segs.length}`);
  for (let i = 1; i < segs.length; i++) {
    assert.ok(segs[i].startMs >= segs[i - 1].endMs, "时间戳必须单调递增");
  }
});

console.log(`\n${passed} 项通过`);

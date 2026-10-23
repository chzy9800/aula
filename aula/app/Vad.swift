import Foundation

/// 断句（VAD）。与浏览器 AudioWorklet、CLI 助手、菜单栏字幕窗**同口径**——
/// 三端切出来的句子边界必须一致，否则同一段录音在网页和 app 里会得到不同分段，
/// 延迟与译文对不上。
///
/// 参数来源：`aula/static/app.js` 的 worklet 与本文件逐行对应；
/// `menubar/main.swift` 里的 VadSegmenter 是同一份逻辑，新 app 换代后它会被废弃。
///
/// 输出帧格式：`<III 小端> startMs, endMs, flags` + PCM16LE
///   - `flags = 0` 定稿帧（入队转写并入库）
///   - `flags = 1` 草稿帧（只带最近 8 秒，边说边出字，不入库）
final class VadSegmenter {

    let targetRate: Double = 16000
    let windowSamples = 320          // 20ms @16k
    let silenceMs: Double
    let minSpeechMs: Double
    let maxSegmentMs: Double

    private var noise = 0.004
    private var speaking = false
    private var chunk: [Int16] = []
    private var segSamples = 0
    private var silenceSamples = 0
    private var startSample = 0
    private var lastVoice = 0
    private var clock = 0
    private var sincePartial = 0
    /// 草稿间隔与草稿音频窗口：转写速度档直接控制这两个值。
    /// 窗口越短 ASR 越快，但上下文越少、残句越多——这是"实时感 vs 准确度"的旋钮。
    private let partialMs: Double
    private let partialWindowSamples: Int

    /// 定稿段（flags=0）
    var onSegment: ((Data) -> Void)?
    /// 草稿段（flags=1）
    var onPartial: ((Data) -> Void)?

    init(silenceMs: Double = 550,
         minSpeechMs: Double = 350,
         maxSegmentMs: Double = 15000,
         partialMs: Double = 900,
         partialWindowSeconds: Double = 8) {
        self.silenceMs = silenceMs
        self.minSpeechMs = minSpeechMs
        self.maxSegmentMs = maxSegmentMs
        self.partialMs = partialMs
        self.partialWindowSamples = Int(partialWindowSeconds * 16000)
    }

    func reset() {
        speaking = false
        chunk = []
        segSamples = 0
        silenceSamples = 0
        sincePartial = 0
        lastVoice = 0
        noise = 0.004
    }

    func feed(samples: [Int16]) {
        var position = 0
        while position + windowSamples <= samples.count {
            handleWindow(Array(samples[position..<position + windowSamples]))
            position += windowSamples
        }
        // 不足一窗的尾巴留到下次，不能丢也不能提前判
        if position < samples.count {
            handleWindow(Array(samples[position...]))
        }
    }

    private func handleWindow(_ window: [Int16]) {
        var sum = 0.0
        for sample in window {
            let value = Double(sample) / 32768.0
            sum += value * value
        }
        let rms = (sum / Double(window.count)).squareRoot()

        if !speaking {
            noise = noise * 0.97 + rms * 0.03
            if rms > max(0.010, noise * 3) {
                speaking = true
                chunk = window
                segSamples = window.count
                silenceSamples = 0
                startSample = clock
                lastVoice = window.count
            }
            clock += window.count
        } else {
            chunk.append(contentsOf: window)
            segSamples += window.count
            clock += window.count
            if rms < max(0.008, noise * 2.5) {
                silenceSamples += window.count
            } else {
                silenceSamples = 0
                lastVoice = segSamples
            }
            let silentMs = Double(silenceSamples) / targetRate * 1000
            let totalMs = Double(segSamples) / targetRate * 1000
            if silentMs >= silenceMs || totalMs >= maxSegmentMs {
                flush()
            } else {
                sincePartial += window.count
                if sincePartial >= Int(partialMs / 1000 * targetRate) {
                    sincePartial = 0
                    emitPartial()
                }
            }
        }
    }

    private func emitPartial() {
        guard speaking, segSamples >= Int(minSpeechMs / 1000 * targetRate) else { return }
        let total = segSamples
        let keep = min(total, partialWindowSamples)
        let skip = total - keep
        let payload = chunk.suffix(keep)
        let startMs = UInt32((Double(startSample + skip) / targetRate) * 1000)
        let endMs = UInt32((Double(startSample + total) / targetRate) * 1000)
        onPartial?(Self.frame(payload: payload, startMs: startMs, endMs: endMs, flags: 1))
    }

    private func flush() {
        guard speaking else { return }
        let total = segSamples
        let minSamples = Int(minSpeechMs / 1000 * targetRate)
        let pad = Int(targetRate * 0.1)
        var keep = min(total, max(lastVoice, 0) + pad)
        if keep < minSamples { keep = total }
        keep = min(keep, total)
        if total >= minSamples {
            let payload = chunk.prefix(keep)
            let startMs = UInt32((Double(startSample) / targetRate) * 1000)
            let endMs = UInt32((Double(startSample + keep) / targetRate) * 1000)
            onSegment?(Self.frame(payload: payload, startMs: startMs, endMs: endMs, flags: 0))
        }
        speaking = false
        chunk = []
        segSamples = 0
        silenceSamples = 0
        sincePartial = 0
        lastVoice = 0
    }

    /// 组帧：12 字节小端头 + PCM16LE
    static func frame(payload: ArraySlice<Int16>, startMs: UInt32, endMs: UInt32, flags: UInt32) -> Data {
        var data = Data(capacity: 12 + payload.count * 2)
        for value in [startMs, endMs, flags] {
            withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
        }
        payload.withUnsafeBufferPointer { buffer in
            data.append(contentsOf: UnsafeRawBufferPointer(buffer))
        }
        return data
    }

    /// 裸 PCM16 数据也走同一个组帧口径（自检与回放用）
    static func frame(pcm: Data, startMs: UInt32, endMs: UInt32, flags: UInt32) -> Data {
        var data = Data(capacity: 12 + pcm.count)
        for value in [startMs, endMs, flags] {
            withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
        }
        data.append(pcm)
        return data
    }
}

import Foundation

// 原生 WS 客户端探针：验证 B 路线（Swift 原生 UI）能否直接对接现有 aula 服务端实时链路。
// 上行：<III 小端> start_ms, end_ms, flags + PCM16LE 16k mono
// 下行：ready / partial / transcript / translation / error / flushed
// 用法: wsprobe <session_id> <pcm_dir> [port]

final class Recorder: @unchecked Sendable {
    private let lock = NSLock()
    private var counts: [String: Int] = [:]
    private var t0 = Date()
    private var timeline: [(Double, String)] = []

    func start() { t0 = Date() }

    func note(_ type: String, detail: String) {
        lock.lock(); defer { lock.unlock() }
        counts[type, default: 0] += 1
        let ms = Date().timeIntervalSince(t0) * 1000
        timeline.append((ms, "\(type) \(detail)"))
    }

    func dump() {
        lock.lock(); defer { lock.unlock() }
        print("--- 消息统计 ---")
        for (k, v) in counts.sorted(by: { $0.key < $1.key }) { print("\(k): \(v)") }
        print("--- 时间线（ms，从首帧发送算）---")
        for (ms, s) in timeline { print(String(format: "%7.0f  %@", ms, s)) }
    }
}

let args = CommandLine.arguments
guard args.count > 2 else { print("usage: wsprobe <session_id> <pcm_dir> [port]"); exit(1) }
let sid = args[1]
let pcmDir = URL(fileURLWithPath: args[2])
let port = args.count > 3 ? args[3] : "8791"

let sem = DispatchSemaphore(value: 0)
let rec = Recorder()

// 载入 PCM 片段
var clips: [Data] = []
for i in 1...3 {
    let u = pcmDir.appendingPathComponent("s\(i).raw")
    if let d = try? Data(contentsOf: u) { clips.append(d) }
}
guard !clips.isEmpty else { print("FAIL: 没读到 PCM"); exit(1) }
print("clips: \(clips.map { $0.count })")

let url = URL(string: "ws://127.0.0.1:\(port)/ws/live?session_id=\(sid)")!
let task = URLSession.shared.webSocketTask(with: url)
task.resume()

func frame(_ pcm: Data, _ start: UInt32, _ end: UInt32, flags: UInt32) -> Data {
    var d = Data()
    for v in [start, end, flags] { var le = v.littleEndian; withUnsafeBytes(of: &le) { d.append(contentsOf: $0) } }
    d.append(pcm)
    return d
}

// 收消息循环
Task {
    while true {
        do {
            let msg = try await task.receive()
            switch msg {
            case .string(let s):
                handle(s)
            case .data(let d):
                rec.note("binary_down", detail: "\(d.count)B")
            @unknown default: break
            }
        } catch {
            print("recv end: \(error.localizedDescription)")
            break
        }
    }
    sem.signal()
}

func handle(_ s: String) {
    guard let d = s.data(using: .utf8),
          let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
          let type = obj["type"] as? String else { return }
    switch type {
    case "ready":
        let asr = (obj["asr"] as? [String: Any])?["provider"] as? String ?? "?"
        let sr = obj["sample_rate"] as? Int ?? 0
        rec.note("ready", detail: "asr=\(asr) sr=\(sr)")
        rec.start()
        Task { await drive() }
    case "partial":
        let src = obj["source"] as? String ?? ""
        let tgt = obj["target"] as? String ?? ""
        rec.note("partial", detail: "src=\"\(src.prefix(48))\" tgt=\"\(tgt.prefix(24))\"")
    case "transcript":
        let u = obj["utterance"] as? [String: Any] ?? [:]
        let src = u["source"] as? String ?? ""
        let tgt = u["target"] as? String ?? ""
        let lat = obj["latency_ms"] as? Int ?? 0
        rec.note("transcript", detail: "asr=\(lat)ms src=\"\(src.prefix(48))\" rough=\"\(tgt.prefix(20))\"")
    case "translation":
        let tgt = obj["target"] as? String ?? ""
        rec.note("translation", detail: "精译=\"\(tgt.prefix(28))\"")
    case "flushed":
        rec.note("flushed", detail: "")
    case "error":
        rec.note("error", detail: obj["message"] as? String ?? "")
    default:
        rec.note(type, detail: "")
    }
}

func drive() async {
    var clock: UInt32 = 0
    for (idx, pcm) in clips.enumerated() {
        // 草稿帧：取前 65% 当"说到一半"
        let cut = pcm.count * 65 / 100
        let draft = pcm.prefix(cut)
        let draftMs = UInt32(cut / 32)  // 16k * 2B = 32B/ms
        try? await task.send(.data(frame(draft, clock, clock + draftMs, flags: 1)))
        try? await Task.sleep(nanoseconds: 700_000_000)

        // 定稿帧：整段
        let fullMs = UInt32(pcm.count / 32)
        try? await task.send(.data(frame(pcm, clock, clock + fullMs, flags: 0)))
        clock += fullMs
        print("sent clip \(idx + 1): \(pcm.count)B")
        try? await Task.sleep(nanoseconds: 2_500_000_000)
    }
    // flush 等 worker 排空
    try? await task.send(.string("{\"flush\":1}"))
    try? await Task.sleep(nanoseconds: 3_000_000_000)
    rec.dump()
    task.cancel(with: .normalClosure, reason: nil)
    sem.signal()
}

// 兜底超时
DispatchQueue.global().asyncAfter(deadline: .now() + 45) {
    print("TIMEOUT")
    rec.dump()
    exit(2)
}

sem.wait()
print("DONE")

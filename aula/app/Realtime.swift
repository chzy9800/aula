import Foundation

/// 实时链路客户端：`/ws/live` 上的双向流。
///
/// 上行：`<III 小端>` startMs / endMs / flags + PCM16LE（见 `Vad.swift`）
/// 下行：ready / partial / transcript / translation / summary / status / error / flushed
///
/// 服务端一行都不用改——这套协议是 0.6/0.7 版本为浏览器和 Swift 助手共同设计的，
/// 原生 UI 直接复用（验证记录见 `docs/b-route-validation.md` V3）。
final class RealtimeClient {

    enum Event {
        case ready(asr: String, model: String, sampleRate: Int)
        case partial(key: Int, source: String, target: String)
        case transcript(seq: Int, key: Int, source: String, target: String, latencyMs: Int)
        /// 流式译文的增量：字幕"逐字长出来"靠它，不是整句替换
        case translationDelta(seq: Int, delta: String)
        case translation(seq: Int, target: String)
        case status(String)
        case error(String)
        case flushed
    }

    /// 事件回到主线程派发——UI 层不需要自己切。
    var onEvent: ((Event) -> Void)?
    var onDisconnect: ((String) -> Void)?

    private var task: URLSessionWebSocketTask?
    private let urlSession = URLSession(configuration: .default)
    private var receiveLoop: Task<Void, Never>?
    private(set) var connected = false

    func connect(baseURL: String, sessionId: String) {
        disconnect()
        guard let url = URL(string: baseURL.replacingOccurrences(of: "http://", with: "ws://")
                            + "/ws/live?session_id=" + sessionId) else {
            emit(.error("WS 地址不合法"))
            return
        }
        let task = urlSession.webSocketTask(with: url)
        self.task = task
        task.resume()
        receiveLoop = Task { [weak self] in
            await self?.receive(in: task)
        }
    }

    private func receive(in task: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            do {
                let message = try await task.receive()
                switch message {
                case .string(let text):
                    handle(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) { handle(text) }
                @unknown default:
                    break
                }
            } catch {
                if !Task.isCancelled {
                    main { self.onDisconnect?(error.localizedDescription) }
                }
                return
            }
        }
    }

    private func handle(_ text: String) {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = obj["type"] as? String else { return }

        switch type {
        case "ready":
            connected = true
            let asr = obj["asr"] as? [String: Any] ?? [:]
            emit(.ready(asr: asr["provider"] as? String ?? "?",
                        model: asr["model"] as? String ?? "?",
                        sampleRate: obj["sample_rate"] as? Int ?? 16000))
        case "partial":
            emit(.partial(key: obj["key"] as? Int ?? 0,
                          source: obj["source"] as? String ?? "",
                          target: obj["target"] as? String ?? ""))
        case "transcript":
            let u = obj["utterance"] as? [String: Any] ?? [:]
            emit(.transcript(seq: u["seq"] as? Int ?? 0,
                             key: obj["key"] as? Int ?? 0,
                             source: u["source"] as? String ?? "",
                             target: u["target"] as? String ?? "",
                             latencyMs: obj["latency_ms"] as? Int ?? 0))
        case "translation_delta":
            emit(.translationDelta(seq: obj["seq"] as? Int ?? 0,
                                   delta: obj["delta"] as? String ?? ""))
        case "translation":
            emit(.translation(seq: obj["seq"] as? Int ?? 0,
                              target: obj["target"] as? String ?? ""))
        case "status":
            emit(.status(obj["message"] as? String ?? ""))
        case "error":
            emit(.error(obj["message"] as? String ?? "未知错误"))
        case "flushed":
            emit(.flushed)
        default:
            break
        }
    }

    func send(_ frame: Data) {
        task?.send(.data(frame)) { _ in }
    }

    /// 让服务端把队列排空后再回 `flushed`——停止录音时用它确认没丢最后几句。
    func flush() {
        task?.send(.string("{\"flush\":1}")) { _ in }
    }

    func disconnect() {
        receiveLoop?.cancel()
        receiveLoop = nil
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        connected = false
    }

    private func emit(_ event: Event) {
        main { self.onEvent?(event) }
    }

    private func main(_ block: @escaping () -> Void) {
        if Thread.isMainThread { block() } else { DispatchQueue.main.async(execute: block) }
    }
}

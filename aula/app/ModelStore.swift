import Foundation
import Combine

/// 本地 whisper 模型的状态与下载。
///
/// 为什么要单独做：切到 turbo 后首次使用会下载 3GB 权重，但界面上什么都没有，
/// 用户只会看到"点开始没反应"——以为坏了。这里把「已下载 / 需下载多少 / 下载到几成」
/// 全摆出来。
final class ModelStore: ObservableObject {

    struct Info: Identifiable, Equatable {
        let name: String
        let downloaded: Bool
        let expectedMB: Double
        let actualMB: Double
        let progress: Double
        let active: Bool
        var id: String { name }

        var label: String {
            switch name {
            case "small": return "small"
            case "medium": return "medium"
            case "large-v3-turbo": return "turbo"
            default: return name
            }
        }

        var statusText: String {
            if downloaded { return String(format: "已下载 %.0f MB", actualMB) }
            if actualMB > 0 { return String(format: "下载中 %.0f / %.0f MB", actualMB, expectedMB) }
            return String(format: "需要下载约 %.0f MB", expectedMB)
        }
    }

    /// 预置静态列表：服务还没起时也要能看到"有哪些模型、各多大"，
    /// 否则用户打开设置只看到一句"读不到模型状态"，等于什么都没显示。
    @Published private(set) var models: [Info] = [
        Info(name: "small", downloaded: false, expectedMB: 950, actualMB: 0, progress: 0, active: false),
        Info(name: "medium", downloaded: false, expectedMB: 3000, actualMB: 0, progress: 0, active: false),
        Info(name: "large-v3-turbo", downloaded: false, expectedMB: 3100, actualMB: 0, progress: 0, active: false),
    ]
    @Published private(set) var current = ""
    @Published private(set) var error = ""
    @Published private(set) var busy = false

    private var pollTask: DispatchSourceTimer?
    private var baseURL: () -> String

    init(baseURL: @escaping () -> String) {
        self.baseURL = baseURL
    }

    func refresh() {
        let url = baseURL() + "/api/models"
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            guard let payload = Sidecar.getAny(url) as? [String: Any],
                  let raw = payload["models"] as? [[String: Any]] else {
                DispatchQueue.main.async { self.error = "读不到模型状态（服务可能还没就绪）" }
                return
            }
            let parsed: [Info] = raw.compactMap { item in
                guard let name = item["name"] as? String else { return nil }
                return Info(
                    name: name,
                    downloaded: (item["downloaded"] as? Bool) ?? false,
                    expectedMB: (item["expected_mb"] as? Double) ?? 0,
                    actualMB: (item["actual_mb"] as? Double) ?? 0,
                    progress: (item["progress"] as? Double) ?? 0,
                    active: (item["active"] as? Bool) ?? false
                )
            }
            DispatchQueue.main.async {
                self.models = parsed
                self.current = (payload["current"] as? String) ?? ""
                self.error = ""
                self.busy = parsed.contains { !$0.downloaded && $0.actualMB > 0 }
                if self.busy { self.startPolling() } else { self.stopPolling() }
            }
        }
    }

    /// 触发后台下载。下载期间每秒刷新一次进度。
    func download(_ name: String) {
        let url = baseURL() + "/api/models/\(name)/download"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            _ = Sidecar.post(url, payload: [:], timeout: 20)
            DispatchQueue.main.async {
                self?.busy = true
                self?.refresh()
                self?.startPolling()
            }
        }
    }

    private func startPolling() {
        guard pollTask == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 1.5, repeating: 1.5)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            self.refresh()
        }
        timer.resume()
        pollTask = timer
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    deinit {
        pollTask?.cancel()
    }
}

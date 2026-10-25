import Foundation
import Combine

/// 翻译服务商与模型选择。
///
/// AirTranslate 那边是一个「Gemini 3.5 Live Translate」下拉；我们这边服务商和模型是
/// 两个维度（同一个服务商下常有好几个模型），所以做成两级选择。
final class ProviderStore: ObservableObject {

    struct Info: Identifiable, Equatable {
        let id: String
        let label: String
        let models: [String]
        let keyRequired: Bool
        let freeNote: String
        var ident: String { id }
    }

    @Published private(set) var providers: [Info] = []
    @Published private(set) var currentProvider = ""
    @Published private(set) var currentModel = ""
    @Published private(set) var error = ""
    @Published private(set) var saving = false

    private let baseURL: () -> String

    init(baseURL: @escaping () -> String) {
        self.baseURL = baseURL
    }

    var current: Info? {
        providers.first { $0.id == currentProvider }
    }

    var modelsForCurrent: [String] {
        current?.models ?? []
    }

    func refresh() {
        let url = baseURL() + "/api/providers"
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            guard let payload = Sidecar.getAny(url) as? [String: Any],
                  let raw = payload["providers"] as? [[String: Any]] else {
                DispatchQueue.main.async { self.error = "读不到服务商列表（服务可能还没就绪）" }
                return
            }
            let parsed: [Info] = raw.compactMap { item in
                guard let id = item["id"] as? String,
                      let label = item["label"] as? String else { return nil }
                return Info(
                    id: id,
                    label: label,
                    models: (item["models"] as? [String]) ?? [],
                    keyRequired: (item["key_required"] as? Bool) ?? true,
                    freeNote: (item["free_note"] as? String) ?? ""
                )
            }
            DispatchQueue.main.async {
                self.providers = parsed
                self.currentProvider = (payload["current_provider"] as? String) ?? ""
                self.currentModel = (payload["current_model"] as? String) ?? ""
                self.error = ""
            }
        }
    }

    /// 切服务商。服务端会用预设补齐 base_url / 模型 / 密钥环境变量。
    func select(provider id: String) {
        guard id != currentProvider else { return }
        put(["provider": id], optimistic: { $0.currentProvider = id })
    }

    /// 切模型（同一服务商下的具体型号）
    func selectModel(_ model: String) {
        guard model != currentModel else { return }
        put(["llm_model": model], optimistic: { $0.currentModel = model })
    }

    private func put(_ payload: [String: Any], optimistic: @escaping (ProviderStore) -> Void) {
        saving = true
        optimistic(self)
        let url = baseURL() + "/api/config"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = Sidecar.put(url, payload: payload, timeout: 20)
            DispatchQueue.main.async {
                guard let self else { return }
                self.saving = false
                if result == nil {
                    self.error = "保存失败：服务端没有响应"
                } else {
                    self.error = ""
                }
                // 无论成败都重新拉一次，让界面回到真实状态
                self.refresh()
            }
        }
    }
}

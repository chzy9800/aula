import SwiftUI
import AppKit

/// 转录库：已保存的课堂列表。对应 AirTranslate 的 Transcript Library。
///
/// 数据直接读服务端 `/api/sessions`，不在 Swift 侧另存一份——库的真相只有一个地方。
final class TranscriptLibraryStore: ObservableObject {

    struct SavedSession: Identifiable, Equatable {
        let id: String
        let title: String
        let createdAt: String
        let utterances: Int
    }

    @Published private(set) var sessions: [SavedSession] = []
    @Published private(set) var loading = false
    @Published private(set) var error: String = ""
    @Published var selected: String?

    private let baseURL: () -> String

    init(baseURL: @escaping () -> String) {
        self.baseURL = baseURL
    }

    func reload() {
        loading = true
        error = ""
        let url = baseURL() + "/api/sessions"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let raw = Sidecar.getAny(url)
            var parsed: [SavedSession] = []
            var failure = ""
            if let items = raw as? [[String: Any]] {
                parsed = items.compactMap { item in
                    guard let id = item["id"] as? String else { return nil }
                    return SavedSession(
                        id: id,
                        title: (item["title"] as? String) ?? "未命名课堂",
                        createdAt: (item["created_at"] as? String) ?? "",
                        utterances: (item["utterance_count"] as? Int) ?? 0
                    )
                }
            } else {
                failure = "读不到课堂列表：服务可能还没就绪。看「打开服务日志」确认"
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.sessions = parsed
                self.error = failure
                self.loading = false
                if self.selected == nil { self.selected = parsed.first?.id }
            }
        }
    }

    func delete(_ id: String) {
        let url = baseURL() + "/api/sessions/\(id)"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            _ = Sidecar.deleteAny(url)
            DispatchQueue.main.async { self?.reload() }
        }
    }

    /// 导出成 Markdown 并让用户选保存位置
    func export(_ session: SavedSession) {
        let url = baseURL() + "/api/sessions/\(session.id)/export?transcript=1"
        DispatchQueue.global(qos: .userInitiated).async {
            guard let text = Sidecar.getString(url), !text.isEmpty else { return }
            DispatchQueue.main.async {
                let panel = NSSavePanel()
                panel.allowedContentTypes = [.plainText]
                let safeTitle = session.title.replacingOccurrences(of: "/", with: "-")
                panel.nameFieldStringValue = "\(safeTitle).md"
                panel.canCreateDirectories = true
                if panel.runModal() == .OK, let target = panel.url {
                    try? text.write(to: target, atomically: true, encoding: .utf8)
                }
            }
        }
    }

    /// 在应用内继续这节课（把它的句子载回上课界面）
    func resume(_ session: SavedSession, into store: ClassStore) {
        store.resume(sessionID: session.id, title: session.title)
    }
}

// MARK: - 视图

struct TranscriptLibraryView: View {

    @ObservedObject var model: TranscriptLibraryStore
    @ObservedObject var classStore: ClassStore

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.3)
            content
        }
        .frame(width: 720, height: 520)
        .onAppear { model.reload() }
    }

    private var header: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text("转录库").font(.system(size: 15, weight: .semibold))
                Text("每节课结束后自动存档，可导出成 Markdown")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if model.loading { ProgressView().controlSize(.small) }
            Button("刷新") { model.reload() }
                .controlSize(.small)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }

    @ViewBuilder
    private var content: some View {
        if !model.error.isEmpty {
            VStack(spacing: 8) {
                Text(model.error)
                    .font(.system(size: 12))
                    .foregroundStyle(.orange)
                    .multilineTextAlignment(.center)
                Button("重试") { model.reload() }.controlSize(.small)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if model.sessions.isEmpty && !model.loading {
            VStack(spacing: 6) {
                Text("还没有保存过课堂").font(.system(size: 13))
                Text("按 ⌥⌘R 听一节课，结束后会自动出现在这里。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollView {
                LazyVStack(spacing: 6) {
                    ForEach(model.sessions) { session in
                        row(session)
                    }
                }
                .padding(14)
            }
        }
    }

    private func row(_ session: TranscriptLibraryStore.SavedSession) -> some View {
        let selected = model.selected == session.id
        return HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(session.title).font(.system(size: 12.5, weight: .medium))
                HStack(spacing: 8) {
                    if !session.createdAt.isEmpty {
                        Text(session.createdAt.replacingOccurrences(of: "T", with: " ").prefix(16))
                            .font(.system(size: 10).monospacedDigit())
                    }
                    Text("\(session.utterances) 句").font(.system(size: 10))
                }
                .foregroundStyle(.tertiary)
            }
            Spacer()
            if selected {
                Button("导出") { model.export(session) }
                    .controlSize(.small)
                Button("继续这节") { model.resume(session, into: classStore) }
                    .controlSize(.small)
                Button("删除") { model.delete(session.id) }
                    .controlSize(.small)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(RoundedRectangle(cornerRadius: 8)
            .fill(selected ? Color.accentColor.opacity(0.12) : Color.primary.opacity(0.04)))
        .contentShape(Rectangle())
        .onTapGesture { model.selected = session.id }
    }
}

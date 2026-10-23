import AppKit
import PDFKit

// PDFView 圈选交互探针（B 路线未验证项之一）。
// 验证：程序化拖选能否触发 selectionChanged、能否拿到选中文本与页面矩形。
// 这是「圈住不懂的地方就能问」的原生实现前提——拿到 string 才能问，拿到 bounds 才能钉回答。
//
// 用法: pdfview_select <file.pdf>

final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var value = 0
    func bump() { lock.lock(); value += 1; lock.unlock() }
}

let args = CommandLine.arguments
guard args.count > 1 else { print("usage: pdfview_select <file.pdf>"); exit(1) }
guard let doc = PDFDocument(url: URL(fileURLWithPath: args[1])) else {
    print("FAIL: 打不开 PDF")
    exit(1)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let view = PDFView(frame: NSRect(x: 0, y: 0, width: 900, height: 700))
view.document = doc
view.autoScales = true
view.displayMode = .singlePageContinuous
view.displayDirection = .vertical

let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 700),
                      styleMask: [.titled], backing: .buffered, defer: false)
window.contentView = view
// 不进屏幕，但要完成布局：否则 page layout 是空的，坐标算不出来
window.orderBack(nil)
view.layoutSubtreeIfNeeded()

let counter = Counter()
var observedText = ""
let token = NotificationCenter.default.addObserver(
    forName: .PDFViewSelectionChanged, object: view, queue: nil
) { note in
    counter.bump()
    if let sel = note.object as? PDFView, let s = sel.currentSelection?.string {
        observedText = s
    }
}

// 让通知派发一轮
func spin(_ seconds: TimeInterval) {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
    }
}
spin(0.3)

print("pages: \(doc.pageCount)")
print("selectionChanged_before: \(counter.value)")

// 拖选第一页标题行附近：从左上到右下
view.goToFirstPage(nil)
spin(0.2)

guard let page = view.currentPage else {
    print("FAIL: 没有当前页")
    exit(1)
}

// ① 按字符粒度拖选——最接近用户"圈住一段"
// API 在 PDFPage 上（PDFView 只有 currentSelection）：page.selection(from:to:)
let p1 = NSPoint(x: 70, y: 730)
let p2 = NSPoint(x: 470, y: 780)
if let sel = page.selection(from: p1, to: p2) {
    view.setCurrentSelection(sel, animate: false)
    spin(0.3)
    let text = sel.string ?? ""
    let b = sel.bounds(for: page)
    print("--- 拖选结果 ---")
    print("text: \"\(text.replacingOccurrences(of: "\n", with: "⏎"))\"")
    print("chars: \(text.count)")
    print("bounds: x=\(String(format: "%.1f", b.minX)) y=\(String(format: "%.1f", b.minY)) w=\(String(format: "%.1f", b.width)) h=\(String(format: "%.1f", b.height))")
    print("pages_in_selection: \(view.currentSelection?.pages.count ?? 0)")
    print("selectionChanged_delta: \(counter.value)")
    // ② 换算成屏幕坐标——高亮视图要贴在这个矩形上
    let inView = view.convert(b, from: page)
    print("view_coords: x=\(String(format: "%.1f", inView.minX)) y=\(String(format: "%.1f", inView.minY)) w=\(String(format: "%.1f", inView.width)) h=\(String(format: "%.1f", inView.height))")
} else {
    print("FAIL: 拖选拿不到 selection")
    exit(1)
}

// ③ 整行/整页选择（用户双击、三击的行为）
if let whole = view.currentSelection?.copy() as? PDFSelection {
    whole.extend(atEnd: 3)
    print("extended_chars: \(whole.string?.count ?? 0)")
}

if observedText.isEmpty {
    print("note: 通知回调没捕获到文本（视图未上屏时正常），但 setCurrentSelection 同步可读")
} else {
    print("notification_text_ok: true")
}

NotificationCenter.default.removeObserver(token)
print("PROBE OK")

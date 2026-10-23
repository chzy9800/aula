import Foundation

/// app 自己的日志。与 `Sidecar` 的 sidecar.log 分开：
/// 那份是 Python 服务端输出，这份是壳层（音频捕获、快捷键、状态机）的输出。
///
/// 为什么必须落盘：`open` 启动的 app 没有终端，stderr 进系统日志，用户看不到、
/// 也贴不出来。"系统声音识别不出"这类问题如果连是静音还是没数据都分不清，
/// 就只能靠猜。
enum AppLog {

    private static let lock = NSLock()
    private static var handle: FileHandle?
    private static let stamp: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    static let fileURL: URL = {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/aula", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("app.log")
    }()

    static func write(_ message: String) {
        let line = "\(stamp.string(from: Date())) \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        lock.lock()
        defer { lock.unlock() }
        if handle == nil {
            let fm = FileManager.default
            if !fm.fileExists(atPath: fileURL.path) {
                fm.createFile(atPath: fileURL.path, contents: nil)
            }
            handle = try? FileHandle(forWritingTo: fileURL)
            handle?.seekToEndOfFile()
        }
        handle?.write(data)
    }
}

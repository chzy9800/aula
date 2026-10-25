import Foundation

/// sidecar 生命周期：找空闲端口 → 拉起内嵌 Python → 健康检查 → 崩溃重启 → 退出清理。
///
/// 为什么不用固定 8780：用户可能已经用浏览器跑着一个 `aula serve`，两个进程抢端口
/// 会静默失败（uvicorn 报错退出，app 侧只看到"连不上"）。端口协商让 app 永远能起来。
///
/// 为什么不用 venv：venv 的 `pyvenv.cfg` 记录构建机的绝对路径，搬进 .app 后解释器
/// 找不到 home。pbs（python-build-standalone）本身是 relocatable 的，把依赖直接装进
/// 它自己的 site-packages，整个目录搬到哪都能跑。
final class Sidecar: ObservableObject {

    enum State: String {
        case stopped, starting, ready, stopping, failed
    }

    /// 状态变化回调（主线程）。保留给命令行自检用——它没有 Combine 运行循环。
    var onStateChange: ((State, String) -> Void)?

    /// 界面要跟着服务状态刷新：启动过程有 0.5–1 秒，不订阅的话界面会一直停在
    /// "未开始"，直到用户碰一下控件才重绘。
    @Published private(set) var state: State = .stopped
    @Published private(set) var statusText: String = "未启动"
    private(set) var port: Int = 0
    private(set) var lastError: String = ""

    private var process: Process?
    private var restarts = 0
    private var stopping = false
    private let queue = DispatchQueue(label: "com.chzy.aula.sidecar")
    private let logURL: URL
    private let logLock = NSLock()
    private var logHandle: FileHandle?

    /// 冷启动耗时（从 spawn 到 /api/health 通），实测指标不靠猜。
    private(set) var coldStartMs: Int = 0

    var baseURL: String { "http://127.0.0.1:\(port)" }

    /// 状态与文案必须一起变。用 didSet 会踩坑：先赋 state 再赋 statusText，
    /// 回调里读到的是旧文案（状态窗会闪一下"运行中"再变"已停止"）。
    private func setState(_ new: State, _ text: String) {
        statusText = text
        state = new
        onStateChange?(new, text)
    }

    init() {
        let logs = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/aula", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        logURL = logs.appendingPathComponent("sidecar.log")
        write("=== 新会话 ===")
    }

    // MARK: - 路径

    /// app bundle 内的 sidecar 运行时；开发态（从源码目录跑）退回仓库里的 pbs。
    private var runtimeRoot: URL? {
        if let res = Bundle.main.resourceURL {
            let bundled = res.appendingPathComponent("python", isDirectory: true)
            if FileManager.default.isExecutableFile(atPath: bundled.appendingPathComponent("bin/python3").path) {
                return res
            }
        }
        let env = ProcessInfo.processInfo.environment["AULA_SIDECAR_ROOT"]
        if let env {
            let url = URL(fileURLWithPath: env)
            if FileManager.default.isExecutableFile(atPath: url.appendingPathComponent("python/bin/python3").path) {
                return url
            }
        }
        return nil
    }

    private var pythonPath: String? {
        runtimeRoot?.appendingPathComponent("python/bin/python3").path
    }

    // MARK: - 启动

    func start() {
        queue.async { [weak self] in
            guard let self else { return }
            guard self.process == nil else { return }
            self.stopping = false
            self.setState(.starting, "正在启动服务…")

            guard let root = self.runtimeRoot, let python = self.pythonPath else {
                self.fail("找不到内嵌 Python 运行时（Contents/Resources/python）")
                return
            }
            guard let freePort = Self.findFreePort(from: 8780, to: 8840) else {
                self.fail("8780–8840 没有空闲端口")
                return
            }
            self.port = freePort

            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: python)
            proc.arguments = ["-m", "aula", "serve", "--host", "127.0.0.1", "--port", "\(freePort)"]
            proc.currentDirectoryURL = root
            var env = ProcessInfo.processInfo.environment
            // 让 python 找到 bundle 里的 aula 源码；不写死绝对路径，跟着 bundle 走
            env["PYTHONPATH"] = root.appendingPathComponent("lib").path
            env["PYTHONUNBUFFERED"] = "1"
            // 模型选择由 app 传下去：small 快但西语不够准，large-v3-turbo 明显更准。
            // 走环境变量而不是配置文件，改完重启服务就生效。
            let chosenModel = UserDefaults.standard.string(forKey: "aula.settings.asrModel") ?? ""
            if !chosenModel.isEmpty {
                env["AULA_ASR_MODEL"] = chosenModel
            }
            // 子进程不该继承 app 的 GUI 相关变量，但保留用户的 AULA_* 覆盖
            env.removeValue(forKey: "__CFBundleIdentifier")
            proc.environment = env

            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe
            // 服务端 stdout/stderr 也进同一个日志文件；崩溃后要看的是它，不是控制台
            pipe.fileHandleForReading.readabilityHandler = { [weak self] fh in
                let data = fh.availableData
                guard !data.isEmpty else { return }
                self?.appendLog(data)
            }

            proc.terminationHandler = { [weak self] p in
                guard let self else { return }
                self.queue.async {
                    // 主动停止不算崩溃，不重启
                    if self.stopping {
                        self.process = nil
                        self.setState(.stopped, "已停止")
                        return
                    }
                    self.write("sidecar 意外退出，status=\(p.terminationStatus)")
                    self.process = nil
                    if self.restarts < 3 {
                        self.restarts += 1
                        self.setState(.starting, "服务异常退出，第 \(self.restarts) 次重试…")
                        self.queue.asyncAfter(deadline: .now() + Double(self.restarts) * 1.5) {
                            self.start()
                        }
                    } else {
                        self.fail("服务连续 3 次退出，已放弃重启")
                    }
                }
            }

            do {
                let t0 = Date()
                try proc.run()
                self.process = proc
                self.write("spawn pid=\(proc.processIdentifier) port=\(freePort)")
                self.waitHealthy(from: t0, attempts: 120)
            } catch {
                self.fail("拉起 sidecar 失败：\(error.localizedDescription)")
            }
        }
    }

    /// 轮询 /api/health，最多 attempts × 0.25s。
    /// 首次启动要加载 whisper 模型，这里给到 30 秒；健康检查不通就报错，不静默等。
    private func waitHealthy(from t0: Date, attempts: Int) {
        var left = attempts
        while left > 0 {
            if stopping { return }
            if let health = Self.get(baseURL + "/api/health", timeout: 1.5) {
                coldStartMs = Int(Date().timeIntervalSince(t0) * 1000)
                restarts = 0
                let asr = (health["asr"] as? [String: Any])?["provider"] as? String ?? "?"
                let model = (health["asr"] as? [String: Any])?["model"] as? String ?? "?"
                write("healthy in \(coldStartMs)ms (port \(port), asr \(asr)/\(model))")
                setState(.ready, "运行中 · \(port) · 转写 \(asr)/\(model)")
                return
            }
            left -= 1
            Thread.sleep(forTimeInterval: 0.25)
        }
        fail("健康检查超时（30s），看 \(logURL.path)")
    }

    // MARK: - 停止

    func stop() {
        queue.async { [weak self] in
            guard let self else { return }
            guard let proc = self.process else {
                self.setState(.stopped, "已停止")
                return
            }
            self.stopping = true
            self.setState(.stopping, "正在停止…")
            proc.terminate()
            // 给 uvicorn 3 秒体面退出（它会关 SQLite 连接），再强杀
            var waited = 0.0
            while proc.isRunning && waited < 3.0 {
                Thread.sleep(forTimeInterval: 0.1)
                waited += 0.1
            }
            if proc.isRunning {
                self.write("SIGTERM 超时，SIGKILL")
                kill(proc.processIdentifier, SIGKILL)
            }
            self.process = nil
            self.write("stopped")
            self.setState(.stopped, "已停止")
        }
    }

    /// 退出前用：发出停止请求并**等到进程真的没了**再返回。
    /// 退出路径上不能只调 stop()——那是异步的，主线程走到 exit 时它可能还没跑完，
    /// uvicorn 来不及关 SQLite，"清理成功"只是碰巧。
    func stopAndWait(timeout: TimeInterval = 4) {
        stop()
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let running = queue.sync { self.process?.isRunning ?? false }
            if !running { return }
            Thread.sleep(forTimeInterval: 0.05)
        }
        write("stopAndWait 超时（\(Int(timeout))s），可能残留 sidecar")
    }

    /// 主动重启（设置界面/状态窗的按钮）。
    func restart() {
        queue.async { [weak self] in
            guard let self else { return }
            if let proc = self.process {
                self.stopping = true
                proc.terminate()
                var waited = 0.0
                while proc.isRunning && waited < 3.0 {
                    Thread.sleep(forTimeInterval: 0.1)
                    waited += 0.1
                }
                if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
                self.process = nil
            }
            self.restarts = 0
            self.stopping = false
            self.start()
        }
    }

    // MARK: - 工具

    private func fail(_ message: String) {
        lastError = message
        write("ERROR: " + message)
        setState(.failed, message)
    }

    private func write(_ line: String) {
        let ts = ISO8601DateFormatter().string(from: Date())
        guard let data = ("\(ts) \(line)\n").data(using: .utf8) else { return }
        appendLog(data)
        FileHandle.standardError.write(data)
    }

    /// 追加写日志。
    /// 坑：`FileHandle(forWritingTo:)` **不会创建文件**，文件不存在时它抛错；
    /// 早先用 `try?` 兜着，结果日志一直静默丢弃——"日志落盘"成了假承诺。
    private func appendLog(_ data: Data) {
        logLock.lock()
        defer { logLock.unlock() }
        if logHandle == nil {
            let fm = FileManager.default
            if !fm.fileExists(atPath: logURL.path) {
                fm.createFile(atPath: logURL.path, contents: nil)
            }
            logHandle = try? FileHandle(forWritingTo: logURL)
            logHandle?.seekToEndOfFile()
        }
        logHandle?.write(data)
    }

    /// 真正试着 bind 一下，而不是查端口表——查表会漏掉刚被释放的端口。
    private static func findFreePort(from: Int, to: Int) -> Int? {
        for candidate in from...to {
            let fd = socket(AF_INET, SOCK_STREAM, 0)
            guard fd >= 0 else { continue }
            defer { close(fd) }
            var yes: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
            var addr = sockaddr_in()
            addr.sin_family = sa_family_t(AF_INET)
            addr.sin_port = in_port_t(UInt16(candidate).bigEndian)
            addr.sin_addr.s_addr = inet_addr("127.0.0.1")
            let bound = withUnsafePointer(to: &addr) { ptr in
                ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            if bound == 0 { return candidate }
        }
        return nil
    }

    static func get(_ urlString: String, timeout: TimeInterval) -> [String: Any]? {
        guard let url = URL(string: urlString) else { return nil }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = "GET"
        var result: [String: Any]?
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, _, _ in
            if let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                result = obj
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 1)
        return result
    }

    static func post(_ urlString: String, payload: [String: Any], timeout: TimeInterval = 8) -> [String: Any]? {
        request(urlString, method: "POST", payload: payload, timeout: timeout)
    }

    /// DELETE（删课堂）
    @discardableResult

    /// PUT（改配置用）
    static func put(_ urlString: String, payload: [String: Any], timeout: TimeInterval = 15) -> [String: Any]? {
        guard let url = URL(string: urlString) else { return nil }
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        var result: [String: Any]?
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                result = obj
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 1)
        return result
    }

    static func deleteAny(_ urlString: String, timeout: TimeInterval = 8) -> Bool {
        guard let url = URL(string: urlString) else { return false }
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = "DELETE"
        var ok = false
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, response, _ in
            if let http = response as? HTTPURLResponse { ok = (200..<300).contains(http.statusCode) }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 1)
        return ok
    }

    /// 取纯文本响应（导出 Markdown 用，不是 JSON）
    static func getString(_ urlString: String, timeout: TimeInterval = 20) -> String? {
        guard let url = URL(string: urlString) else { return nil }
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = "GET"
        var text: String?
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data { text = String(data: data, encoding: .utf8) }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 1)
        return text
    }

    /// GET 版本（读课堂列表、课堂详情）。返回字典或数组都包成 Any。
    static func getAny(_ urlString: String, timeout: TimeInterval = 8) -> Any? {
        guard let url = URL(string: urlString) else { return nil }
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = "GET"
        var result: Any?
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data { result = try? JSONSerialization.jsonObject(with: data) }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 1)
        return result
    }

    private static func request(_ urlString: String, method: String,
                                payload: [String: Any], timeout: TimeInterval) -> [String: Any]? {
        guard let url = URL(string: urlString) else { return nil }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        var result: [String: Any]?
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, _, _ in
            if let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                result = obj
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 1)
        return result
    }
}

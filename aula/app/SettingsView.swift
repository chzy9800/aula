import SwiftUI
import AppKit
import AVFoundation
import Speech

/// 设置窗口的分类状态。与 AirTranslate 的 `SettingsSidebar` 对应。
final class SettingsModel: ObservableObject {

    enum Category: String, CaseIterable, Identifiable {
        case session, transcription, translation, caption, floating, speech, permissions, about

        var id: String { rawValue }

        func title(_ lang: L10n.Lang) -> String {
            switch self {
            case .session: return L10n.t("会话", "Session", lang: lang)
            case .transcription: return L10n.t("转写", "Transcription", lang: lang)
            case .translation: return L10n.t("翻译", "Translation", lang: lang)
            case .caption: return L10n.t("字幕", "Captions", lang: lang)
            case .floating: return L10n.t("浮窗", "Floating", lang: lang)
            case .speech: return L10n.t("语音输出", "Voice", lang: lang)
            case .permissions: return L10n.t("权限", "Permissions", lang: lang)
            case .about: return L10n.t("关于", "About", lang: lang)
            }
        }

        var symbol: String {
            switch self {
            case .session: return "record.circle"
            case .transcription: return "waveform"
            case .translation: return "character.bubble"
            case .caption: return "textformat.size"
            case .floating: return "rectangle.on.rectangle"
            case .speech: return "speaker.wave.2"
            case .permissions: return "lock.shield"
            case .about: return "info.circle"
            }
        }

        /// 页头说明：每页一句，告诉用户这一堆设置到底管什么
        func subtitle(_ lang: L10n.Lang) -> String {
            switch self {
            case .session:
                return L10n.t("会话怎么开始、结束后转录放哪", "How a session starts and where transcripts go", lang: lang)
            case .transcription:
                return L10n.t("断句节奏与单句长度", "Sentence pacing and segment length", lang: lang)
            case .translation:
                return L10n.t("译文从哪里来、术语怎么保", "Where translation comes from and how terms are kept", lang: lang)
            case .caption:
                return L10n.t("字号、配色、对齐——主界面和浮窗共用", "Size, colour, alignment — shared by both windows", lang: lang)
            case .floating:
                return L10n.t("可拖动的浮动字幕窗", "The draggable floating caption window", lang: lang)
            case .speech:
                return L10n.t("把译文读出来", "Speak translated captions aloud", lang: lang)
            case .permissions:
                return L10n.t("三项系统权限，缺哪个这里能看到", "Three system permissions and their live status", lang: lang)
            case .about:
                return L10n.t("版本与安装位置", "Version and install location", lang: lang)
            }
        }
    }

    @Published var category: Category = .session
}

// MARK: - 设置窗口

struct SettingsView: View {

    @ObservedObject var model: SettingsModel
    @ObservedObject var settings: AppSettings
    @ObservedObject var style: CaptionStyle
    @ObservedObject var classStore: ClassStore

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider().opacity(0.4)
            VStack(alignment: .leading, spacing: 0) {
                pageHeader
                if classStore.phase == .listening || classStore.phase == .starting {
                    HStack(spacing: 6) {
                        Image(systemName: "lock.fill").font(.system(size: 10))
                        Text("正在听课：转写与翻译设置将在停止后生效")
                            .font(.system(size: 10.5))
                    }
                    .foregroundStyle(.orange)
                    .padding(.horizontal, 20)
                    .padding(.bottom, 8)
                }
                Divider().opacity(0.25)
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        page
                    }
                    .padding(20)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .frame(width: 760, height: 540)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    // MARK: 侧边栏

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(SettingsModel.Category.allCases) { category in
                Button {
                    model.category = category
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: category.symbol)
                            .font(.system(size: 12))
                            .frame(width: 16)
                        Text(category.title(settings.uiLanguage)).font(.system(size: 12.5))
                        Spacer()
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .background(
                        RoundedRectangle(cornerRadius: 7)
                            .fill(model.category == category ? Theme.teal.opacity(0.16) : Color.clear)
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundStyle(model.category == category ? Theme.teal : Color.primary)
            }
            Spacer()
        }
        .padding(10)
        .frame(width: 168)
    }

    private var pageHeader: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(model.category.title(settings.uiLanguage)).font(.system(size: 15, weight: .semibold))
            Text(model.category.subtitle(settings.uiLanguage))
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: 各页

    @ViewBuilder
    private var page: some View {
        switch model.category {
        case .session: sessionPage
        case .transcription: transcriptionPage
        case .translation: translationPage
        case .caption: captionPage
        case .floating: floatingPage
        case .speech: speechPage
        case .permissions: permissionsPage
        case .about: aboutPage
        }
    }

    private var sessionPage: some View {
        Group {
            settingRow("每次开始新建课堂") {
                Toggle("", isOn: $settings.newSessionEachStart).labelsHidden()
            }
            hint(settings.newSessionEachStart
                 ? "每次按开始都新建一节课，历史留在转录库里。"
                 : "续用上一次的课堂，新句子接着往那节课里写。")
            settingRow("结束后保存转录") {
                Toggle("", isOn: $settings.saveTranscripts).labelsHidden()
            }
            hint("关闭后只做实时显示，不写入数据库（听课记录也就无从生成）。")
            divider
            keyValue("当前课堂", classStore.sessionTitle.isEmpty ? "未开始" : classStore.sessionTitle)
            keyValue("服务", classStore.serviceStatus)
        }
    }

    private var transcriptionPage: some View {
        Group {
            settingRow("转写速度") {
                Picker("", selection: $settings.transcriptionSpeed) {
                    ForEach(AppSettings.TranscriptionSpeed.allCases) { speed in
                        Text(speed.label).tag(speed)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 220)
            }
            hint(settings.transcriptionSpeed.note)
            divider
            sliderRow("段落静音阈值", value: $settings.paragraphSilenceMs, range: 300...2000, unit: "ms",
                      note: "停顿多久算一句说完了。小=切得碎、上屏快；大=句子完整、等待久。")
            sliderRow("最短语音", value: $settings.minSpeechMs, range: 150...800, unit: "ms",
                      note: "短于这个长度不算一句，用来滤掉咳嗽、翻页这类杂音。")
            sliderRow("单句上限", value: $settings.maxSegmentMs, range: 5000...30000, unit: "ms",
                      note: "一口气说太久时强制断开，避免一句话等到下课。")
            hint("改动在下次「开始听课」时生效。")
        }
    }

    private var translationPage: some View {
        Group {
            settingRow("输出模式") {
                Picker("", selection: $settings.outputMode) {
                    ForEach(AppSettings.OutputMode.allCases) { mode in
                        Text(mode.label).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 280)
            }
            hint(settings.outputMode.note)
            divider
            settingRow("翻译优先级") {
                Picker("", selection: $settings.translationPriority) {
                    ForEach(AppSettings.TranslationPriority.allCases) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 200)
            }
            hint(settings.translationPriority.note)
            divider
            keyValue("翻译方式", "大模型流式翻译（逐字上屏）")
            keyValue("当前模型", classStore.asrLabel.isEmpty ? "未连接" : classStore.asrLabel)
            hint("译文只有一个来源，逐字长出来，不会整句替换——这是为了避免屏幕上反复闪。")
            divider
            Text("术语越准，译文越稳。术语表来自服务端配置；同一节课内同一术语不会换译法。")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            hint("想改服务商/密钥：菜单 → 打开服务日志 里能看到当前生效的配置；改法见 docs/providers.md。")
        }
    }

    private var captionPage: some View {
        Group {
            Text("预设").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
            HStack(spacing: 8) {
                ForEach(CaptionStyle.presets) { preset in
                    Button {
                        style.apply(preset: preset)
                    } label: {
                        VStack(spacing: 4) {
                            RoundedRectangle(cornerRadius: 5)
                                .fill(Color(hex: preset.textHex))
                                .frame(width: 22, height: 22)
                                .overlay(RoundedRectangle(cornerRadius: 5)
                                    .strokeBorder(Color.primary.opacity(0.15)))
                            Text(preset.shortName)
                                .font(.system(size: 9))
                                .foregroundStyle(.secondary)
                        }
                        .padding(6)
                        .background(RoundedRectangle(cornerRadius: 7)
                            .fill(style.activePresetID == preset.id
                                  ? Theme.teal.opacity(0.15) : Color.clear))
                    }
                    .buttonStyle(.plain)
                    .help(preset.name)
                }
            }
            divider
            sliderRow("译文字号", value: $style.fontSize, range: 11...30, unit: "pt", note: nil)
            sliderRow("原文字号", value: $style.sourceFontSize, range: 9...20, unit: "pt", note: nil)
            hexRow("译文颜色", hex: $style.textHex)
            hexRow("原文颜色", hex: $style.sourceHex)
            settingRow("对齐") {
                Picker("", selection: $style.alignment) {
                    ForEach(CaptionStyle.TextAlignmentOption.allCases) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 140)
            }
            settingRow("文字描边") {
                Toggle("", isOn: $style.outline).labelsHidden()
            }
            hint("亮背景（幻灯片、白底 PDF）上开描边更清楚。")
            settingRow("滚动边缘渐隐") {
                Toggle("", isOn: $style.fadeMask).labelsHidden()
            }
        }
    }

    private var floatingPage: some View {
        Group {
            sliderRow("浮窗字号缩放", value: $style.floatingFontScale, range: 0.7...2.0, unit: "×",
                      note: "相对字幕字号。投屏演示时可以调大。")
            sliderRow("浮窗不透明度", value: $style.floatingOpacity, range: 0.3...1.0, unit: "", note: nil)
            sliderRow("保留句数", value: Binding(
                get: { Double(style.floatingKeepLines) },
                set: { style.floatingKeepLines = Int($0) }
            ), range: 1...6, unit: "句", note: "浮窗只留最近几句，太多会挡住画面。")
            divider
            Text("拖动浮窗任意位置可移动，右下角可缩放。位置和尺寸会自动记住。")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            hint("菜单 → 浮窗 → 重置位置与尺寸 可以把窗口拉回右上角默认大小。")
        }
    }

    private var speechPage: some View {
        Group {
            settingRow("朗读译文") {
                Toggle("", isOn: $settings.speechEnabled).labelsHidden()
            }
            hint("只在译文定稿后朗读。逐字朗读会把一句话读成几十段碎片。")
            if settings.speechEnabled {
                settingRow("音色") {
                    Picker("", selection: $settings.speechVoiceID) {
                        Text("系统默认（中文）").tag("")
                        ForEach(SpeechOutput.availableVoices(), id: \.id) { voice in
                            Text("\(voice.name) · \(voice.lang)").tag(voice.id)
                        }
                    }
                    .labelsHidden()
                    .frame(width: 240)
                }
                sliderRow("音量", value: $settings.speechVolume, range: 0...1, unit: "", note: nil)
                sliderRow("语速", value: $settings.speechRate, range: 0.3...1.2, unit: "×", note: nil)
                Button("试听一句") {
                    classStore.speech.speak("这是译文朗读的音色与语速示例。", settings: settings)
                }
                .controlSize(.small)
            } else {
                hint("打开后可以调音色、音量与语速。")
            }
        }
    }

    private var permissionsPage: some View {
        Group {
            permissionRow("麦克风",
                          granted: AVCaptureDevice.authorizationStatus(for: .audio) == .authorized,
                          detail: "用麦克风听课时需要",
                          settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
            permissionRow("屏幕录制",
                          granted: SystemAudioSource.hasPermission(),
                          detail: "抓系统声音（网课）时必需。授权后需重启 app 才真正放行",
                          settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")
            permissionRow("语音识别",
                          granted: SFSpeechRecognizer.authorizationStatus() == .authorized,
                          detail: "本机语音能力（可选，aula 用自有 whisper 时不依赖）",
                          settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition")
            divider
            hint("权限记在发起进程上。命令行跑出来的「已授权」不代表 app 已授权。")
            Button("重新检查") { model.objectWillChange.send() }
                .controlSize(.small)
        }
    }

    private var aboutPage: some View {
        Group {
            settingRow(L10n.t("界面语言", "Interface language", lang: settings.uiLanguage)) {
                Picker("", selection: $settings.uiLanguage) {
                    ForEach(L10n.Lang.allCases) { lang in
                        Text(lang.label).tag(lang)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 160)
            }
            hint(L10n.t("目前覆盖设置界面与主要按钮；主界面的实时状态文案仍是中文。",
                        "Covers the settings window and main buttons. Live status text stays in Chinese for now.",
                        lang: settings.uiLanguage))
            divider
            keyValue("版本", Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.8.0")
            keyValue("应用路径", Bundle.main.bundlePath)
            keyValue("日志", AppLog.fileURL.path)
            divider
            Text("上课：⌥⌘R 开始/停止 · ⌘1 上课界面 · ⌘2 字幕浮窗")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
    }

    // MARK: 控件工厂

    private var divider: some View {
        Divider().opacity(0.25).padding(.vertical, 2)
    }

    private func settingRow<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        HStack {
            Text(title).font(.system(size: 12))
            Spacer()
            content()
        }
    }

    private func sliderRow(_ title: String, value: Binding<Double>,
                           range: ClosedRange<Double>, unit: String, note: String?) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title).font(.system(size: 12))
                Spacer()
                Text(unit.isEmpty
                     ? String(format: "%.2f", value.wrappedValue)
                     : String(format: "%.0f%@", value.wrappedValue, unit))
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Slider(value: value, in: range)
            if let note {
                Text(note).font(.system(size: 10.5)).foregroundStyle(.tertiary)
            }
        }
    }

    private func hexRow(_ title: String, hex: Binding<String>) -> some View {
        HStack(spacing: 8) {
            Text(title).font(.system(size: 12))
            Spacer()
            RoundedRectangle(cornerRadius: 4)
                .fill(hex.wrappedValue.isEmpty ? Color.primary.opacity(0.15) : Color(hex: hex.wrappedValue))
                .frame(width: 18, height: 18)
                .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Color.primary.opacity(0.15)))
            TextField("自动", text: hex)
                .textFieldStyle(.roundedBorder)
                .frame(width: 92)
                .font(.system(size: 11, design: .monospaced))
        }
    }

    private func keyValue(_ key: String, _ value: String) -> some View {
        HStack(alignment: .top) {
            Text(key).font(.system(size: 12)).foregroundStyle(.secondary)
            Spacer()
            Text(value)
                .font(.system(size: 11.5))
                .multilineTextAlignment(.trailing)
                .textSelection(.enabled)
        }
    }

    private func hint(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 10.5))
            .foregroundStyle(.tertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func permissionRow(_ title: String, granted: Bool,
                               detail: String, settingsURL: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: granted ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(granted ? Color.green : Color.orange)
                .font(.system(size: 13))
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 12, weight: .medium))
                Text(detail).font(.system(size: 10.5)).foregroundStyle(.secondary)
            }
            Spacer()
            Button("系统设置") {
                if let url = URL(string: settingsURL) { NSWorkspace.shared.open(url) }
            }
            .controlSize(.small)
        }
    }
}

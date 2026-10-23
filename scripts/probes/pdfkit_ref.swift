import Foundation
import PDFKit
import AppKit

// PDFKit 探针 v2：1:1 移植 reader.js groupBlocks 的四条规则，与 pdf.js 口径对比。
// 规则：① y 差 < height*0.5 归同行 ② height > median*1.25 判标题
//      ③ gapRatio < (标题?2.05:1.62) 且字号变化 ≤25% 才合并 ④ 相邻两块都是标题且后块 <34 字符并回

guard CommandLine.arguments.count > 1 else { print("usage: pdfkit_ref <file.pdf> [pageLimit]"); exit(1) }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let pageLimit = CommandLine.arguments.count > 2 ? Int(CommandLine.arguments[2]) ?? 5 : 5
guard let doc = PDFDocument(url: url) else { print("FAIL open"); exit(1) }
print("pages_total: \(doc.pageCount)")

struct PLine { var y: CGFloat; var height: CGFloat; var text: String }

func linesOf(_ page: PDFPage) -> [PLine] {
    let pageH = page.bounds(for: .mediaBox).height
    guard let sel = page.selection(for: page.bounds(for: .mediaBox)) else { return [] }
    var out: [PLine] = []
    for s in sel.selectionsByLine() {
        guard let t = s.string, !t.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
        let b = s.bounds(for: page)
        // 转屏幕坐标（y 向下递增），与 pdf.js 口径一致
        let y = pageH - b.maxY
        let h = max(b.height, 1)
        out.append(PLine(y: y, height: h, text: t.trimmingCharacters(in: .whitespaces)))
    }
    return out.sorted { $0.y < $1.y }
}

func groupBlocks(_ lines: [PLine]) -> [(text: String, heading: Bool)] {
    var ls = lines
    let heights = ls.map { $0.height }.sorted()
    let median = heights.isEmpty ? 10 : heights[heights.count / 2]
    var isHeading = [Bool](repeating: false, count: ls.count)
    for i in 0..<ls.count { isHeading[i] = ls[i].height > median * 1.25 }

    var blocks: [(text: String, y: CGFloat, lastY: CGFloat, height: CGFloat, heading: Bool)] = []
    for (i, line) in ls.enumerated() {
        if var last = blocks.last {
            let gapRatio = (line.y - last.lastY) / max(line.height, last.height)
            let scaleChanged = abs(line.height - last.height) / max(line.height, last.height) > 0.25
            let gapLimit: CGFloat = last.heading ? 2.05 : 1.62
            if gapRatio < gapLimit && !scaleChanged {
                let sep = last.text.range(of: "[。.!?;:：；]$", options: .regularExpression) != nil ? "\n" : " "
                last.text += sep + line.text
                last.lastY = line.y
                blocks[blocks.count - 1] = last
                continue
            }
        }
        blocks.append((text: line.text, y: line.y, lastY: line.y, height: line.height, heading: isHeading[i]))
    }
    var i = blocks.count - 1
    while i > 0 {
        let cur = blocks[i], prev = blocks[i - 1]
        if cur.heading && prev.heading && cur.text.trimmingCharacters(in: .whitespaces).count < 34 {
            blocks[i - 1].text = (prev.text + " " + cur.text).trimmingCharacters(in: .whitespaces)
            blocks[i - 1].lastY = cur.lastY
            blocks.remove(at: i)
        }
        i -= 1
    }
    _ = ls
    return blocks.filter { $0.text.trimmingCharacters(in: .whitespaces).count > 1 }.map { ($0.text, $0.heading) }
}

var totalChars = 0, totalParas = 0
var firstParas: [String] = []
var perPage: [(Int, Int, Int)] = []

for p in 0..<min(pageLimit, doc.pageCount) {
    guard let page = doc.page(at: p) else { continue }
    let ls = linesOf(page)
    let blocks = groupBlocks(ls)
    let chars = blocks.reduce(0) { $0 + $1.text.count }
    totalChars += chars
    totalParas += blocks.count
    perPage.append((p, ls.count, blocks.count))
    if p == 0 { firstParas = blocks.prefix(6).map { $0.text } }
}
print("chars: \(totalChars) paras: \(totalParas)")
print("avg_chars_per_para: \(totalParas > 0 ? totalChars / totalParas : 0)")
print("--- 首页前 6 段（PDFKit 口径）---")
for (i, t) in firstParas.enumerated() {
    print("[\(i)] \(t.count > 110 ? String(t.prefix(110)) + "…" : t)")
}
print("--- 逐页 ---")
for (p, l, b) in perPage { print("page \(p): lines=\(l) paras=\(b)") }

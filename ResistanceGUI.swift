import Cocoa
import Darwin
import Foundation
import UniformTypeIdentifiers

struct DataPoint {
    let time: TimeInterval
    let value: Double
    let rawValue: Double
    let rawLine: String
}

struct RawSample {
    let index: Int
    let receivedAt: Date
    let elapsed: TimeInterval
    let rawLine: String
    let parsedResistance: Double?
}

enum SerialError: LocalizedError {
    case openFailed(String)
    case configureFailed(String)
    case unsupportedBaud(Int)
    case readFailed(String)

    var errorDescription: String? {
        switch self {
        case .openFailed(let message):
            return "无法打开串口：\(message)"
        case .configureFailed(let message):
            return "串口配置失败：\(message)"
        case .unsupportedBaud(let baud):
            return "暂不支持波特率：\(baud)"
        case .readFailed(let message):
            return "串口读取失败：\(message)"
        }
    }
}

enum ResistanceParser {
    static func parse(_ raw: String) -> Double? {
        let line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if line.isEmpty {
            return nil
        }

        let unitPattern = "(MΩ|MOhm|Mohm|兆欧|mΩ|mohm|毫欧|[kK]Ω|[kK][oO][hH][mM]|千欧|Ω|[oO][hH][mM]|欧姆|欧)?"
        let numberPattern = "([-+]?(?:\\d[\\d,]*\\.?\\d*|\\.\\d+)(?:[eE][-+]?\\d+)?)"
        let labelPattern = "(?:R_s|R_S|r_s|r_S|R[sS]|r[sS]|R|r|resistance|Resistance|RESISTANCE|res|Res|RES|电阻)"
        let markedPattern = "(?:^|[\\s,;])\(labelPattern)\\s*[:=]\\s*\(numberPattern)\\s*\(unitPattern)"
        let firstPattern = "\(numberPattern)\\s*\(unitPattern)"

        guard let match = firstMatch(pattern: markedPattern, in: line) ?? firstMatch(pattern: firstPattern, in: line) else {
            return nil
        }

        let numericText = match.number.replacingOccurrences(of: ",", with: "")
        guard let value = Double(numericText) else {
            return nil
        }

        switch match.unit {
        case "MΩ", "MOhm", "Mohm", "兆欧":
            return value * 1_000_000
        case "mΩ", "mohm", "毫欧":
            return value / 1_000
        case "kΩ", "KΩ", "kohm", "Kohm", "KOHM", "千欧":
            return value * 1_000
        default:
            return value
        }
    }

    private static func firstMatch(pattern: String, in line: String) -> (number: String, unit: String)? {
        guard let regex = try? NSRegularExpression(pattern: pattern) else {
            return nil
        }
        let range = NSRange(line.startIndex..<line.endIndex, in: line)
        guard let match = regex.firstMatch(in: line, range: range), match.numberOfRanges >= 2 else {
            return nil
        }
        guard let numberRange = Range(match.range(at: 1), in: line) else {
            return nil
        }
        var unit = ""
        if match.numberOfRanges >= 3, let unitRange = Range(match.range(at: 2), in: line) {
            unit = String(line[unitRange])
        }
        return (String(line[numberRange]), unit)
    }
}

final class SerialReader {
    private var fd: Int32 = -1
    private var isRunning = false
    private var readThread: Thread?

    var onLine: ((String) -> Void)?
    var onBytes: ((Int) -> Void)?
    var onError: ((Error) -> Void)?

    static func availablePorts() -> [String] {
        let deviceNames = (try? FileManager.default.contentsOfDirectory(atPath: "/dev")) ?? []
        return deviceNames
            .filter { $0.hasPrefix("cu.") }
            .map { "/dev/\($0)" }
            .sorted()
    }

    func start(portPath: String, baudRate: Int) throws {
        stop()

        let opened = open(portPath, O_RDWR | O_NOCTTY | O_NONBLOCK)
        guard opened >= 0 else {
            throw SerialError.openFailed(String(cString: strerror(errno)))
        }

        fd = opened
        do {
            try configure(fd: fd, baudRate: baudRate)
        } catch {
            close(fd)
            fd = -1
            throw error
        }

        isRunning = true
        readThread = Thread { [weak self] in
            self?.readLoop()
        }
        readThread?.name = "Resistance Serial Reader"
        readThread?.start()
    }

    func stop() {
        isRunning = false
        if fd >= 0 {
            close(fd)
            fd = -1
        }
        readThread = nil
    }

    private func configure(fd: Int32, baudRate: Int) throws {
        guard let speed = baudConstant(for: baudRate) else {
            throw SerialError.unsupportedBaud(baudRate)
        }

        var options = termios()
        guard tcgetattr(fd, &options) == 0 else {
            throw SerialError.configureFailed(String(cString: strerror(errno)))
        }

        cfmakeraw(&options)
        guard cfsetspeed(&options, speed) == 0 else {
            throw SerialError.configureFailed(String(cString: strerror(errno)))
        }

        options.c_cflag |= tcflag_t(CLOCAL | CREAD)
        options.c_cflag &= ~tcflag_t(CSIZE)
        options.c_cflag |= tcflag_t(CS8)
        options.c_cflag &= ~tcflag_t(PARENB)
        options.c_cflag &= ~tcflag_t(CSTOPB)

        guard tcsetattr(fd, TCSANOW, &options) == 0 else {
            throw SerialError.configureFailed(String(cString: strerror(errno)))
        }
    }

    private func baudConstant(for baudRate: Int) -> speed_t? {
        switch baudRate {
        case 9_600: return speed_t(B9600)
        case 19_200: return speed_t(B19200)
        case 38_400: return speed_t(B38400)
        case 57_600: return speed_t(B57600)
        case 115_200: return speed_t(B115200)
        case 230_400: return speed_t(B230400)
        default: return nil
        }
    }

    private func readLoop() {
        var pendingLine = ""
        var lastByteTime = Date()
        let flushInterval: TimeInterval = 0.08
        var buffer = [UInt8](repeating: 0, count: 1024)

        while isRunning {
            if fd < 0 {
                break
            }

            let count = buffer.withUnsafeMutableBytes { rawBuffer -> Int in
                guard let base = rawBuffer.baseAddress else { return -1 }
                return read(fd, base, rawBuffer.count)
            }

            if count > 0 {
                lastByteTime = Date()
                DispatchQueue.main.async { [weak self] in
                    self?.onBytes?(count)
                }

                let chunk = String(decoding: buffer.prefix(count), as: UTF8.self)
                for character in chunk {
                    if character == "\n" || character == "\r" {
                        let line = pendingLine
                        pendingLine = ""
                        if !line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                            DispatchQueue.main.async { [weak self] in
                                self?.onLine?(line)
                            }
                        }
                    } else {
                        pendingLine.append(character)
                    }
                }
            } else if count == 0 || errno == EAGAIN {
                if !pendingLine.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                   Date().timeIntervalSince(lastByteTime) >= flushInterval {
                    let line = pendingLine
                    pendingLine = ""
                    DispatchQueue.main.async { [weak self] in
                        self?.onLine?(line)
                    }
                }
                usleep(5_000)
            } else {
                let message = String(cString: strerror(errno))
                DispatchQueue.main.async { [weak self] in
                    self?.onError?(SerialError.readFailed(message))
                }
                break
            }
        }
    }
}

final class GraphView: NSView {
    var points: [DataPoint] = [] {
        didSet { needsDisplay = true }
    }
    var windowSeconds: TimeInterval = 120 {
        didSet { needsDisplay = true }
    }
    var displayWindowSeconds: TimeInterval? {
        didSet {
            panXOffset = 0
            panYOffset = 0
            clampHorizontalPan()
            needsDisplay = true
        }
    }
    var autoScale = true {
        didSet { needsDisplay = true }
    }
    var yGridStep: Double? {
        didSet { needsDisplay = true }
    }
    var yMin: Double = 0 {
        didSet { needsDisplay = true }
    }
    var yMax: Double = 1_000 {
        didSet { needsDisplay = true }
    }
    var unitMode = "kohm" {
        didSet { needsDisplay = true }
    }
    var zoomScale: Double = 1 {
        didSet {
            zoomScale = min(max(zoomScale, 1), 16)
            if zoomScale <= 1, displayWindowSeconds == nil {
                panXOffset = 0
                panYOffset = 0
            } else {
                clampHorizontalPan()
                if zoomScale <= 1 {
                    panYOffset = 0
                }
            }
            needsDisplay = true
        }
    }

    private var trackingAreaRef: NSTrackingArea?
    private var hoveredPoint: DataPoint?
    private var hoverLocation: NSPoint?
    private var lastDragLocation: NSPoint?
    private var panXOffset: Double = 0
    private var panYOffset: Double = 0

    override var isFlipped: Bool { true }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let trackingAreaRef {
            removeTrackingArea(trackingAreaRef)
        }
        let area = NSTrackingArea(
            rect: bounds,
            options: [.activeInKeyWindow, .mouseMoved, .mouseEnteredAndExited, .inVisibleRect],
            owner: self
        )
        addTrackingArea(area)
        trackingAreaRef = area
    }

    override func mouseMoved(with event: NSEvent) {
        let location = convert(event.locationInWindow, from: nil)
        updateHover(at: location)
    }

    override func mouseExited(with event: NSEvent) {
        hoveredPoint = nil
        hoverLocation = nil
        needsDisplay = true
    }

    override func mouseDown(with event: NSEvent) {
        let location = convert(event.locationInWindow, from: nil)
        lastDragLocation = plotRect().contains(location) ? location : nil
    }

    override func mouseDragged(with event: NSEvent) {
        let location = convert(event.locationInWindow, from: nil)
        guard let previous = lastDragLocation else {
            lastDragLocation = plotRect().contains(location) ? location : nil
            return
        }

        panChartBy(dx: location.x - previous.x, dy: location.y - previous.y)
        lastDragLocation = location
        updateHover(at: location)
    }

    override func mouseUp(with event: NSEvent) {
        lastDragLocation = nil
    }

    override func magnify(with event: NSEvent) {
        let location = convert(event.locationInWindow, from: nil)
        guard plotRect().contains(location) else {
            return
        }
        let scale = max(0.2, 1 + Double(event.magnification))
        zoomScale = min(max(zoomScale * scale, 1), 16)
        clampHorizontalPan()
        updateHover(at: location)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)

        NSColor(calibratedRed: 0.98, green: 0.99, blue: 1.0, alpha: 1).setFill()
        bounds.fill()

        let plot = plotRect()
        drawGrid(in: plot)

        let visible = visiblePoints()
        let range = chartRange(for: visible)
        let displayPoints = visible.filter { $0.time >= range.minX && $0.time <= range.maxX }
        guard displayPoints.count >= 2 else {
            drawEmptyMessage(in: bounds, hasData: !visible.isEmpty)
            return
        }

        let path = NSBezierPath()
        path.lineWidth = 2.6

        for (index, point) in displayPoints.enumerated() {
            let location = pointLocation(point, in: plot, range: range)
            if index == 0 {
                path.move(to: location)
            } else {
                path.line(to: location)
            }
        }

        NSColor(calibratedRed: 0.08, green: 0.48, blue: 0.45, alpha: 1).setStroke()
        path.stroke()

        if let latest = displayPoints.last {
            let location = pointLocation(latest, in: plot, range: range)
            NSColor(calibratedRed: 0.9, green: 0.45, blue: 0.25, alpha: 1).setFill()
            NSBezierPath(ovalIn: NSRect(x: location.x - 4, y: location.y - 4, width: 8, height: 8)).fill()
        }

        drawHoverTooltip(in: plot, range: range)
    }

    private func visiblePoints() -> [DataPoint] {
        guard let latest = points.last?.time else {
            return []
        }
        let maxX = max(windowSeconds, latest)
        return points.filter { $0.time <= maxX }
    }

    private func chartRange(for visible: [DataPoint]) -> (minX: Double, maxX: Double, minY: Double, maxY: Double) {
        let latest = visible.last?.time ?? 0
        let fullMaxX = fullMaxX(latest: latest)
        let xSpan = currentXSpan(fullMaxX: fullMaxX)
        if zoomScale <= 1, displayWindowSeconds == nil {
            panXOffset = 0
            panYOffset = 0
        }
        let maxPanOffset = max(0, fullMaxX - xSpan)
        panXOffset = min(max(panXOffset, 0), maxPanOffset)
        let maxX = zoomScale <= 1 ? fullMaxX : fullMaxX - panXOffset
        let minX = zoomScale <= 1 ? 0 : max(0, maxX - xSpan)
        let scoped = visible.filter { $0.time >= minX && $0.time <= maxX }
        let values = scoped.isEmpty ? visible.map(\.value) : scoped.map(\.value)
        var minY = yMin
        var maxY = yMax

        if let step = yGridStep, step > 0, !values.isEmpty {
            let range = customYRange(values: values, step: step, minX: minX, maxX: maxX)
            return applyYZoom(to: range)
        } else if autoScale {
            minY = values.min() ?? 0
            maxY = values.max() ?? 1
            if minY == maxY {
                let pad = max(abs(minY) * 0.05, 1)
                minY -= pad
                maxY += pad
            } else {
                let pad = (maxY - minY) * 0.12
                minY -= pad
                maxY += pad
            }
        } else if minY >= maxY {
            minY = 0
            maxY = 1
        }

        return applyYZoom(to: (minX, maxX, minY, maxY))
    }

    private func customYRange(values: [Double], step: Double, minX: Double, maxX: Double) -> (minX: Double, maxX: Double, minY: Double, maxY: Double) {
        let dataMin = values.min() ?? 0
        let dataMax = values.max() ?? dataMin
        let minimumSpan = step * 5
        let dataSpan = max(dataMax - dataMin, step)
        let intervalCount = max(5, ceil(dataSpan / step))
        let span = max(minimumSpan, intervalCount * step)
        let center = (dataMin + dataMax) / 2
        var minY = floor((center - span / 2) / step) * step
        var maxY = minY + span

        while dataMin < minY {
            minY -= step
            maxY -= step
        }
        while dataMax > maxY {
            minY += step
            maxY += step
        }

        return (minX, maxX, minY, maxY)
    }

    private func applyYZoom(to range: (minX: Double, maxX: Double, minY: Double, maxY: Double)) -> (minX: Double, maxX: Double, minY: Double, maxY: Double) {
        guard zoomScale > 1, range.maxY > range.minY else {
            return range
        }
        let center = (range.minY + range.maxY) / 2
        let span = max((range.maxY - range.minY) / zoomScale, 0.000001)
        return (range.minX, range.maxX, center - span / 2 + panYOffset, center + span / 2 + panYOffset)
    }

    private func drawGrid(in plot: NSRect) {
        let visible = visiblePoints()
        let range = chartRange(for: visible)
        let resolvedUnit = resolveUnit(for: unitMode, values: visible.map(\.value))
        let scale = unitScale(for: resolvedUnit)

        NSColor(calibratedWhite: 0.72, alpha: 0.35).setStroke()
        let grid = NSBezierPath()

        for index in 0...6 {
            let ratio = CGFloat(index) / 6
            let x = plot.minX + ratio * plot.width
            grid.move(to: NSPoint(x: x, y: plot.minY))
            grid.line(to: NSPoint(x: x, y: plot.maxY))
            let seconds = range.minX + Double(ratio) * (range.maxX - range.minX)
            drawLabel(formatTimeAxisLabel(seconds), at: NSPoint(x: x - 14, y: plot.maxY + 14))
        }

        for value in yTickValues(for: range) {
            let ratio = CGFloat((range.maxY - value) / max(range.maxY - range.minY, 0.001))
            let y = plot.minY + ratio * plot.height
            grid.move(to: NSPoint(x: plot.minX, y: y))
            grid.line(to: NSPoint(x: plot.maxX, y: y))
            drawLabel(axisLabel(value / scale.factor, step: yGridStep.map { $0 / scale.factor }), at: NSPoint(x: 12, y: y - 7))
        }

        grid.lineWidth = 1
        grid.stroke()

        NSColor(calibratedWhite: 0.22, alpha: 0.75).setStroke()
        let axis = NSBezierPath()
        axis.move(to: NSPoint(x: plot.minX, y: plot.minY))
        axis.line(to: NSPoint(x: plot.minX, y: plot.maxY))
        axis.line(to: NSPoint(x: plot.maxX, y: plot.maxY))
        axis.stroke()

        drawTitle("电阻 \(scale.label)", at: NSPoint(x: plot.minX, y: 12))
        drawTitle("时间", at: NSPoint(x: plot.maxX - 34, y: bounds.maxY - 24))
    }

    private func drawEmptyMessage(in rect: NSRect, hasData: Bool = false) {
        let text = hasData ? "当前缩放范围内数据点不足。" : "连接串口或启动模拟数据后，曲线会显示在这里。"
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 15),
            .foregroundColor: NSColor.secondaryLabelColor
        ]
        let size = text.size(withAttributes: attrs)
        let point = NSPoint(x: rect.midX - size.width / 2, y: rect.midY - size.height / 2)
        text.draw(at: point, withAttributes: attrs)
    }

    private func plotRect() -> NSRect {
        bounds.insetBy(dx: 70, dy: 38)
    }

    private func pointLocation(_ point: DataPoint, in plot: NSRect, range: (minX: Double, maxX: Double, minY: Double, maxY: Double)) -> NSPoint {
        let x = plot.minX + CGFloat((point.time - range.minX) / max(range.maxX - range.minX, 0.001)) * plot.width
        let yRatio = (point.value - range.minY) / max(range.maxY - range.minY, 0.001)
        let y = plot.maxY - CGFloat(yRatio) * plot.height
        return NSPoint(x: x, y: y)
    }

    private func updateHover(at location: NSPoint) {
        let plot = plotRect()
        guard plot.contains(location) else {
            hoveredPoint = nil
            hoverLocation = nil
            needsDisplay = true
            return
        }

        let visible = visiblePoints()
        let range = chartRange(for: visible)
        let displayPoints = visible.filter { $0.time >= range.minX && $0.time <= range.maxX }
        var bestPoint: DataPoint?
        var bestDistance = CGFloat.greatestFiniteMagnitude

        for point in displayPoints {
            let pointLocation = pointLocation(point, in: plot, range: range)
            let dx = pointLocation.x - location.x
            let dy = pointLocation.y - location.y
            let distance = sqrt(dx * dx + dy * dy)
            if distance < bestDistance {
                bestDistance = distance
                bestPoint = point
            }
        }

        if bestDistance <= 14 {
            hoveredPoint = bestPoint
            hoverLocation = location
        } else {
            hoveredPoint = nil
            hoverLocation = nil
        }
        needsDisplay = true
    }

    private func panChartBy(dx: CGFloat, dy: CGFloat) {
        let range = chartRange(for: visiblePoints())
        let latest = points.last?.time ?? 0
        let fullMaxX = fullMaxX(latest: latest)
        let canPanX = (range.maxX - range.minX) < fullMaxX - 0.001
        guard zoomScale > 1 || canPanX else {
            return
        }
        let plot = plotRect()
        guard plot.width > 0, plot.height > 0 else {
            return
        }
        if canPanX {
            panXOffset += Double(dx / plot.width) * max(range.maxX - range.minX, 0.001)
        }
        if zoomScale > 1 {
            panYOffset += Double(dy / plot.height) * max(range.maxY - range.minY, 0.001)
        }
        clampHorizontalPan()
        needsDisplay = true
    }

    private func clampHorizontalPan() {
        let latest = points.last?.time ?? 0
        let fullMaxX = fullMaxX(latest: latest)
        let xSpan = currentXSpan(fullMaxX: fullMaxX)
        guard xSpan < fullMaxX else {
            panXOffset = 0
            if zoomScale <= 1 {
                panYOffset = 0
            }
            return
        }
        let maxPanOffset = max(0, fullMaxX - xSpan)
        panXOffset = min(max(panXOffset, 0), maxPanOffset)
    }

    func resetViewport() {
        zoomScale = 1
        panXOffset = 0
        panYOffset = 0
        needsDisplay = true
    }

    private func fullMaxX(latest: Double) -> Double {
        if let displayWindowSeconds {
            return max(displayWindowSeconds, latest, 1)
        }
        return max(windowSeconds, latest, 1)
    }

    private func currentXSpan(fullMaxX: Double) -> Double {
        let baseSpan = displayWindowSeconds.map { min(max($0, 1), fullMaxX) } ?? fullMaxX
        return max(1, min(baseSpan / zoomScale, fullMaxX))
    }

    private func drawHoverTooltip(in plot: NSRect, range: (minX: Double, maxX: Double, minY: Double, maxY: Double)) {
        guard let point = hoveredPoint else {
            return
        }

        let pointOnChart = pointLocation(point, in: plot, range: range)
        NSColor(calibratedRed: 0.9, green: 0.45, blue: 0.25, alpha: 1).setFill()
        NSBezierPath(ovalIn: NSRect(x: pointOnChart.x - 5, y: pointOnChart.y - 5, width: 10, height: 10)).fill()

        let text = "时间：\(formatTimeAxisLabel(point.time))\n电阻：\(formatResistance(point.rawValue, preferredUnit: "kohm"))"
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 12),
            .foregroundColor: NSColor.labelColor
        ]
        let textSize = text.boundingRect(
            with: NSSize(width: 180, height: 80),
            options: [.usesLineFragmentOrigin],
            attributes: attrs
        ).size
        let boxSize = NSSize(width: textSize.width + 18, height: textSize.height + 14)
        let anchor = hoverLocation ?? pointOnChart
        var origin = NSPoint(x: anchor.x + 14, y: anchor.y - boxSize.height - 8)
        if origin.x + boxSize.width > bounds.maxX - 8 {
            origin.x = anchor.x - boxSize.width - 14
        }
        if origin.y < bounds.minY + 8 {
            origin.y = anchor.y + 14
        }

        let box = NSRect(origin: origin, size: boxSize)
        NSColor(calibratedWhite: 1, alpha: 0.96).setFill()
        NSBezierPath(roundedRect: box, xRadius: 6, yRadius: 6).fill()
        NSColor(calibratedWhite: 0.2, alpha: 0.22).setStroke()
        NSBezierPath(roundedRect: box, xRadius: 6, yRadius: 6).stroke()
        text.draw(in: box.insetBy(dx: 9, dy: 7), withAttributes: attrs)
    }

    private func yTickValues(for range: (minX: Double, maxX: Double, minY: Double, maxY: Double)) -> [Double] {
        guard let step = yGridStep, step > 0 else {
            return (0...5).map { index in
                let ratio = Double(index) / 5
                return range.maxY - ratio * (range.maxY - range.minY)
            }
        }

        let intervalCount = max(1, Int(round((range.maxY - range.minY) / step)))
        let drawEvery = max(1, Int(ceil(Double(intervalCount) / 12.0)))
        let displayedStep = step * Double(drawEvery)
        let start = ceil(range.minY / displayedStep) * displayedStep
        var values: [Double] = []
        var value = start
        while value <= range.maxY + displayedStep * 0.001 {
            values.append(value)
            value += displayedStep
        }
        return values.isEmpty ? [range.minY, range.maxY] : values
    }

    private func drawLabel(_ text: String, at point: NSPoint) {
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 11),
            .foregroundColor: NSColor.secondaryLabelColor
        ]
        text.draw(at: point, withAttributes: attrs)
    }

    private func drawTitle(_ text: String, at point: NSPoint) {
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.boldSystemFont(ofSize: 12),
            .foregroundColor: NSColor.labelColor
        ]
        text.draw(at: point, withAttributes: attrs)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let serialReader = SerialReader()
    private var window: NSWindow!
    private let graphView = GraphView()
    private let portPopup = NSPopUpButton()
    private let baudPopup = NSPopUpButton()
    private let connectButton = NSButton(title: "连接", target: nil, action: nil)
    private let disconnectButton = NSButton(title: "断开", target: nil, action: nil)
    private let simulateButton = NSButton(title: "模拟数据", target: nil, action: nil)
    private let clearButton = NSButton(title: "清空", target: nil, action: nil)
    private let exportPDFButton = NSButton(title: "导出PDF", target: nil, action: nil)
    private let exportRawCSVButton = NSButton(title: "导出记录CSV", target: nil, action: nil)
    private let zoomInButton = NSButton(title: "放大", target: nil, action: nil)
    private let zoomOutButton = NSButton(title: "缩小", target: nil, action: nil)
    private let resetZoomButton = NSButton(title: "重置缩放", target: nil, action: nil)
    private let statusLabel = NSTextField(labelWithString: "未连接")
    private let currentLabel = NSTextField(labelWithString: "当前：--")
    private let averageLabel = NSTextField(labelWithString: "平均：--")
    private let minLabel = NSTextField(labelWithString: "最小：--")
    private let maxLabel = NSTextField(labelWithString: "最大：--")
    private let bytesLabel = NSTextField(labelWithString: "收到字节：0")
    private let parsedLabel = NSTextField(labelWithString: "解析成功：0")
    private let rawLabel = NSTextField(labelWithString: "保存记录：0")
    private let autosaveLabel = NSTextField(labelWithString: "自动CSV：未开始")
    private let windowSecondsField = NSTextField(string: "24")
    private let sampleIntervalField = NSTextField(string: "60")
    private let xAxisHoursField = NSTextField(string: "6")
    private let yGridStepField = NSTextField(string: "")
    private let experimentTimeField = NSTextField(string: "")
    private let plantIdField = NSTextField(string: "绿萝01")
    private let temperatureField = NSTextField(string: "")
    private let humidityField = NSTextField(string: "")
    private let electrodePopup = NSPopUpButton()
    private let soilPopup = NSPopUpButton()
    private let autoScaleButton = NSButton(checkboxWithTitle: "y 轴自动缩放", target: nil, action: nil)
    private let logView = NSTextView()

    private var dataPoints: [DataPoint] = []
    private var rawSamples: [RawSample] = []
    private var startDate: Date?
    private var rawStartDate: Date?
    private var lastRecordedAt: Date?
    private var rawBytes = 0
    private var rawRecords = 0
    private var simulationTimer: Timer?
    private var measurementStopTimer: Timer?
    private var autosaveURL: URL?
    private var autosaveHandle: FileHandle?
    private var hasAutoStopped = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        refreshPorts()
        configureSerialCallbacks()
        updateStats()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopSimulation()
        cancelMeasurementStopTimer()
        serialReader.stop()
        closeAutosaveFile()
    }

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 160, y: 120, width: 1180, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "实时电阻曲线 GUI"
        window.minSize = NSSize(width: 920, height: 680)

        let root = NSStackView()
        root.orientation = .vertical
        root.spacing = 12
        root.edgeInsets = NSEdgeInsets(top: 18, left: 18, bottom: 18, right: 18)
        root.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "实时电阻曲线")
        title.font = NSFont.boldSystemFont(ofSize: 26)

        let subtitle = NSTextField(labelWithString: "本地 macOS GUI，直接读取硬件串口数据并绘制电阻-时间曲线。")
        subtitle.font = NSFont.systemFont(ofSize: 13)
        subtitle.textColor = .secondaryLabelColor

        let titleStack = NSStackView(views: [title, subtitle])
        titleStack.orientation = .vertical
        titleStack.spacing = 4
        root.addArrangedSubview(titleStack)

        root.addArrangedSubview(makeControlBar())
        root.addArrangedSubview(makeExperimentBar())
        root.addArrangedSubview(makeStatsBar())

        graphView.translatesAutoresizingMaskIntoConstraints = false
        graphView.wantsLayer = true
        graphView.layer?.backgroundColor = NSColor.white.cgColor
        graphView.layer?.borderColor = NSColor.separatorColor.cgColor
        graphView.layer?.borderWidth = 1
        graphView.layer?.cornerRadius = 8
        root.addArrangedSubview(graphView)
        graphView.heightAnchor.constraint(greaterThanOrEqualToConstant: 340).isActive = true

        let logScroll = NSScrollView()
        logScroll.hasVerticalScroller = true
        logScroll.borderType = .bezelBorder
        logView.isEditable = false
        logView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        logView.textColor = NSColor(calibratedWhite: 0.9, alpha: 1)
        logView.backgroundColor = NSColor(calibratedRed: 0.07, green: 0.09, blue: 0.14, alpha: 1)
        logScroll.documentView = logView
        root.addArrangedSubview(logScroll)
        logScroll.heightAnchor.constraint(equalToConstant: 130).isActive = true

        window.contentView = NSView()
        window.contentView?.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor)
        ])

        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func makeControlBar() -> NSStackView {
        baudPopup.addItems(withTitles: ["9600", "19200", "38400", "57600", "115200", "230400"])
        baudPopup.selectItem(withTitle: "115200")

        connectButton.target = self
        connectButton.action = #selector(connectSerial)
        connectButton.bezelStyle = .rounded

        disconnectButton.target = self
        disconnectButton.action = #selector(disconnectSerial)
        disconnectButton.isEnabled = false

        let refreshButton = NSButton(title: "刷新串口", target: self, action: #selector(refreshPorts))

        simulateButton.target = self
        simulateButton.action = #selector(toggleSimulation)

        clearButton.target = self
        clearButton.action = #selector(clearData)

        exportPDFButton.target = self
        exportPDFButton.action = #selector(exportPDF)

        exportRawCSVButton.target = self
        exportRawCSVButton.action = #selector(exportRawCSV)

        zoomInButton.target = self
        zoomInButton.action = #selector(zoomInGraph)
        zoomOutButton.target = self
        zoomOutButton.action = #selector(zoomOutGraph)
        resetZoomButton.target = self
        resetZoomButton.action = #selector(resetGraphZoom)

        autoScaleButton.state = .on
        autoScaleButton.target = self
        autoScaleButton.action = #selector(updateGraphOptions)

        windowSecondsField.placeholderString = "24"
        windowSecondsField.alignment = .right
        windowSecondsField.target = self
        windowSecondsField.action = #selector(updateGraphOptions)

        sampleIntervalField.placeholderString = "60"
        sampleIntervalField.alignment = .right
        sampleIntervalField.target = self
        sampleIntervalField.action = #selector(updateGraphOptions)

        xAxisHoursField.placeholderString = "6"
        xAxisHoursField.alignment = .right
        xAxisHoursField.target = self
        xAxisHoursField.action = #selector(updateGraphOptions)

        yGridStepField.placeholderString = "自动"
        yGridStepField.alignment = .right
        yGridStepField.target = self
        yGridStepField.action = #selector(updateGraphOptions)

        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = 8
        stack.alignment = .centerY
        stack.distribution = .fill

        stack.addArrangedSubview(NSTextField(labelWithString: "串口"))
        stack.addArrangedSubview(portPopup)
        stack.addArrangedSubview(refreshButton)
        stack.addArrangedSubview(NSTextField(labelWithString: "波特率"))
        stack.addArrangedSubview(baudPopup)
        stack.addArrangedSubview(connectButton)
        stack.addArrangedSubview(disconnectButton)
        stack.addArrangedSubview(autoScaleButton)
        stack.addArrangedSubview(makeFlexibleSpacer())
        stack.addArrangedSubview(zoomOutButton)
        stack.addArrangedSubview(zoomInButton)
        stack.addArrangedSubview(resetZoomButton)
        stack.addArrangedSubview(exportRawCSVButton)
        stack.addArrangedSubview(exportPDFButton)
        stack.addArrangedSubview(simulateButton)
        stack.addArrangedSubview(clearButton)

        return stack
    }

    private func makeExperimentBar() -> NSStackView {
        if experimentTimeField.stringValue.isEmpty {
            experimentTimeField.stringValue = defaultExperimentTime()
        }
        electrodePopup.addItems(withTitles: ["叶片两点", "叶柄-叶片", "茎段两点", "自定义"])
        soilPopup.addItems(withTitles: ["正常", "偏干", "干燥", "湿润", "浇水后"])

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 8
        stack.alignment = .leading
        stack.distribution = .fill

        let timingRow = NSStackView()
        timingRow.orientation = .horizontal
        timingRow.spacing = 8
        timingRow.alignment = .centerY

        timingRow.addArrangedSubview(NSTextField(labelWithString: "实验时间"))
        timingRow.addArrangedSubview(experimentTimeField)
        experimentTimeField.widthAnchor.constraint(equalToConstant: 142).isActive = true

        timingRow.addArrangedSubview(NSTextField(labelWithString: "测试时长(h)"))
        timingRow.addArrangedSubview(windowSecondsField)
        windowSecondsField.widthAnchor.constraint(equalToConstant: 76).isActive = true

        timingRow.addArrangedSubview(NSTextField(labelWithString: "记录间隔(s)"))
        timingRow.addArrangedSubview(sampleIntervalField)
        sampleIntervalField.widthAnchor.constraint(equalToConstant: 64).isActive = true

        timingRow.addArrangedSubview(NSTextField(labelWithString: "横轴范围(h)"))
        timingRow.addArrangedSubview(xAxisHoursField)
        xAxisHoursField.widthAnchor.constraint(equalToConstant: 58).isActive = true

        timingRow.addArrangedSubview(NSTextField(labelWithString: "植物编号"))
        timingRow.addArrangedSubview(plantIdField)
        plantIdField.widthAnchor.constraint(equalToConstant: 78).isActive = true
        timingRow.addArrangedSubview(makeFlexibleSpacer())

        let environmentRow = NSStackView()
        environmentRow.orientation = .horizontal
        environmentRow.spacing = 8
        environmentRow.alignment = .centerY

        environmentRow.addArrangedSubview(NSTextField(labelWithString: "温度°C"))
        environmentRow.addArrangedSubview(temperatureField)
        temperatureField.widthAnchor.constraint(equalToConstant: 56).isActive = true

        environmentRow.addArrangedSubview(NSTextField(labelWithString: "湿度%"))
        environmentRow.addArrangedSubview(humidityField)
        humidityField.widthAnchor.constraint(equalToConstant: 56).isActive = true

        environmentRow.addArrangedSubview(NSTextField(labelWithString: "电极位置"))
        environmentRow.addArrangedSubview(electrodePopup)

        environmentRow.addArrangedSubview(NSTextField(labelWithString: "土壤状态"))
        environmentRow.addArrangedSubview(soilPopup)

        environmentRow.addArrangedSubview(NSTextField(labelWithString: "Y格差(Ω)"))
        environmentRow.addArrangedSubview(yGridStepField)
        yGridStepField.widthAnchor.constraint(equalToConstant: 72).isActive = true
        environmentRow.addArrangedSubview(makeFlexibleSpacer())

        stack.addArrangedSubview(timingRow)
        stack.addArrangedSubview(environmentRow)

        return stack
    }

    private func makeStatsBar() -> NSStackView {
        statusLabel.font = NSFont.boldSystemFont(ofSize: 13)
        statusLabel.textColor = .secondaryLabelColor

        let stack = NSStackView(views: [
            statusLabel,
            makeSeparator(),
            currentLabel,
            averageLabel,
            minLabel,
            maxLabel,
            makeSeparator(),
            bytesLabel,
            parsedLabel,
            rawLabel,
            autosaveLabel
        ])
        stack.orientation = .horizontal
        stack.spacing = 12
        stack.alignment = .centerY
        return stack
    }

    private func makeSeparator() -> NSView {
        let view = NSBox()
        view.boxType = .separator
        return view
    }

    private func makeFlexibleSpacer() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    @objc private func refreshPorts() {
        let selected = portPopup.selectedItem?.title
        portPopup.removeAllItems()
        let ports = SerialReader.availablePorts()
        if ports.isEmpty {
            portPopup.addItem(withTitle: "未发现串口")
            portPopup.isEnabled = false
            connectButton.isEnabled = false
        } else {
            portPopup.addItems(withTitles: ports)
            portPopup.isEnabled = true
            connectButton.isEnabled = true
            if let selected, ports.contains(selected) {
                portPopup.selectItem(withTitle: selected)
            }
        }
    }

    private func configureSerialCallbacks() {
        serialReader.onBytes = { [weak self] count in
            guard let self else { return }
            self.rawBytes += count
            self.updateStats()
        }
        serialReader.onLine = { [weak self] line in
            self?.handleSerialLine(line)
        }
        serialReader.onError = { [weak self] error in
            self?.setStatus("读取失败：\(error.localizedDescription)", connected: false)
        }
    }

    @objc private func connectSerial() {
        guard portPopup.isEnabled, let port = portPopup.selectedItem?.title, !port.hasPrefix("未发现") else {
            setStatus("没有可用串口", connected: false)
            return
        }
        stopSimulation()
        let baud = Int(baudPopup.selectedItem?.title ?? "115200") ?? 115_200

        do {
            hasAutoStopped = false
            try serialReader.start(portPath: port, baudRate: baud)
            setStatus("已连接：\(port)", connected: true)
            connectButton.isEnabled = false
            disconnectButton.isEnabled = true
        } catch {
            setStatus(error.localizedDescription, connected: false)
        }
    }

    @objc private func disconnectSerial() {
        serialReader.stop()
        cancelMeasurementStopTimer()
        let savedFile = autosaveURL?.lastPathComponent
        closeAutosaveFile()
        if let savedFile {
            setStatus("未连接，CSV 已保存：\(savedFile)", connected: false)
        } else {
            setStatus("未连接", connected: false)
        }
        connectButton.isEnabled = portPopup.isEnabled
        disconnectButton.isEnabled = false
    }

    private func handleSerialLine(_ line: String) {
        guard !hasAutoStopped else {
            return
        }
        let resistance = ResistanceParser.parse(line)
        guard let resistance else {
            appendLog("未解析 <= \(showControlCharacters(line))")
            updateStats()
            return
        }
        let now = Date()
        guard shouldRecordSample(at: now) else {
            return
        }
        if let startDate, now.timeIntervalSince(startDate) > measurementDurationSeconds() {
            autoStopMeasurement()
            return
        }
        appendRawSample(rawLine: line, parsedResistance: resistance, receivedAt: now)
        appendDataPoint(resistance: resistance, rawLine: line, receivedAt: now)
    }

    private func appendDataPoint(resistance: Double, rawLine: String, receivedAt now: Date = Date()) {
        guard !hasAutoStopped else {
            return
        }
        if startDate == nil {
            startDate = now
            scheduleMeasurementStopTimer()
        }
        let elapsed = now.timeIntervalSince(startDate!)
        let duration = measurementDurationSeconds()
        guard elapsed <= duration else {
            autoStopMeasurement()
            return
        }
        dataPoints.append(DataPoint(time: elapsed, value: resistance, rawValue: resistance, rawLine: rawLine))

        if dataPoints.count > 20_000 {
            dataPoints.removeFirst(dataPoints.count - 20_000)
        }

        graphView.points = dataPoints
        appendLog("\(formatResistance(resistance, preferredUnit: "kohm")) <= \(showControlCharacters(rawLine))")
        updateStats()

        if elapsed >= duration {
            autoStopMeasurement()
        }
    }

    @objc private func toggleSimulation() {
        if simulationTimer != nil {
            stopSimulation()
            return
        }
        serialReader.stop()
        cancelMeasurementStopTimer()
        hasAutoStopped = false
        connectButton.isEnabled = portPopup.isEnabled
        disconnectButton.isEnabled = false
        setStatus("模拟数据中", connected: true)
        simulateButton.title = "停止模拟"

        var phase = 0.0
        simulationTimer = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: true) { [weak self] _ in
            guard let self else { return }
            phase += 0.16
            let baseline = 820 + 180 * sin(phase / 2.5)
            let ripple = 38 * sin(phase * 2.3)
            let noise = Double.random(in: -12...12)
            let value = max(0.1, baseline + ripple + noise)
            let rawLine = "R=\(trim(value)) ohm"
            let now = Date()
            guard self.shouldRecordSample(at: now) else {
                return
            }
            if let startDate = self.startDate, now.timeIntervalSince(startDate) > self.measurementDurationSeconds() {
                self.autoStopMeasurement()
                return
            }
            self.appendRawSample(rawLine: rawLine, parsedResistance: value, receivedAt: now)
            self.appendDataPoint(resistance: value, rawLine: rawLine, receivedAt: now)
        }
    }

    private func shouldRecordSample(at now: Date) -> Bool {
        let interval = sampleIntervalSeconds()
        guard interval > 0 else {
            return true
        }
        guard let lastRecordedAt else {
            return true
        }
        return now.timeIntervalSince(lastRecordedAt) >= interval
    }

    private func appendRawSample(rawLine: String, parsedResistance: Double?, receivedAt now: Date = Date()) {
        if rawStartDate == nil {
            rawStartDate = now
        }
        lastRecordedAt = now
        rawRecords += 1
        let elapsed = now.timeIntervalSince(rawStartDate!)
        let sample = RawSample(
            index: rawRecords,
            receivedAt: now,
            elapsed: elapsed,
            rawLine: rawLine,
            parsedResistance: parsedResistance
        )
        rawSamples.append(sample)
        if rawSamples.count > 20_000 {
            rawSamples.removeFirst(rawSamples.count - 20_000)
        }
        appendAutosaveSample(sample)
    }

    private func stopSimulation() {
        simulationTimer?.invalidate()
        simulationTimer = nil
        simulateButton.title = "模拟数据"
    }

    @objc private func clearData() {
        dataPoints.removeAll()
        rawSamples.removeAll()
        startDate = nil
        rawStartDate = nil
        lastRecordedAt = nil
        hasAutoStopped = false
        cancelMeasurementStopTimer()
        closeAutosaveFile()
        autosaveURL = nil
        autosaveLabel.stringValue = "自动CSV：未开始"
        rawBytes = 0
        rawRecords = 0
        graphView.points = []
        logView.string = ""
        updateStats()
    }

    @objc private func updateGraphOptions() {
        applyGraphOptions()
        scheduleMeasurementStopTimer()
    }

    @objc private func zoomInGraph() {
        graphView.zoomScale = min(graphView.zoomScale * 1.5, 16)
    }

    @objc private func zoomOutGraph() {
        graphView.zoomScale = max(graphView.zoomScale / 1.5, 1)
    }

    @objc private func resetGraphZoom() {
        graphView.resetViewport()
    }

    private func applyGraphOptions() {
        graphView.windowSeconds = measurementDurationSeconds()
        let xWindow = xAxisWindowSeconds()
        if graphView.displayWindowSeconds != xWindow {
            graphView.displayWindowSeconds = xWindow
        }
        graphView.autoScale = autoScaleButton.state == .on
        let gridStep = Double(yGridStepField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 0
        graphView.yGridStep = gridStep > 0 ? gridStep : nil
    }

    private func updateStats() {
        applyGraphOptions()
        let visible = visiblePoints()
        parsedLabel.stringValue = "解析成功：\(dataPoints.count)"
        rawLabel.stringValue = "保存记录：\(rawRecords)"
        bytesLabel.stringValue = "收到字节：\(rawBytes)"

        guard !visible.isEmpty else {
            currentLabel.stringValue = "当前：--"
            averageLabel.stringValue = "平均：--"
            minLabel.stringValue = "最小：--"
            maxLabel.stringValue = "最大：--"
            return
        }

        let values = visible.map(\.rawValue)
        let latest = values.last ?? 0
        let average = values.reduce(0, +) / Double(values.count)
        currentLabel.stringValue = "当前：\(formatResistance(latest, preferredUnit: "kohm"))"
        averageLabel.stringValue = "平均：\(formatResistance(average, preferredUnit: "kohm"))"
        minLabel.stringValue = "最小：\(formatResistance(values.min() ?? 0, preferredUnit: "kohm"))"
        maxLabel.stringValue = "最大：\(formatResistance(values.max() ?? 0, preferredUnit: "kohm"))"
    }

    private func visiblePoints() -> [DataPoint] {
        guard let latest = dataPoints.last?.time else {
            return []
        }
        let seconds = measurementDurationSeconds()
        let maxX = max(seconds, latest)
        return dataPoints.filter { $0.time <= maxX }
    }

    private func measurementDurationSeconds() -> TimeInterval {
        let hours = Double(windowSecondsField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 24
        let seconds = hours * 3_600
        return max(5, min(86_400, seconds))
    }

    private func sampleIntervalSeconds() -> TimeInterval {
        let seconds = Double(sampleIntervalField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 60
        return max(0, seconds)
    }

    private func xAxisWindowSeconds() -> TimeInterval? {
        let trimmed = xAxisHoursField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let hours = Double(trimmed), hours > 0 else {
            return nil
        }
        return min(measurementDurationSeconds(), max(5, hours * 3_600))
    }

    private func scheduleMeasurementStopTimer() {
        measurementStopTimer?.invalidate()
        measurementStopTimer = nil
        guard let startDate, !hasAutoStopped else {
            return
        }

        let remaining = measurementDurationSeconds() - Date().timeIntervalSince(startDate)
        guard remaining > 0 else {
            autoStopMeasurement()
            return
        }

        measurementStopTimer = Timer.scheduledTimer(withTimeInterval: remaining, repeats: false) { [weak self] _ in
            self?.autoStopMeasurement()
        }
    }

    private func cancelMeasurementStopTimer() {
        measurementStopTimer?.invalidate()
        measurementStopTimer = nil
    }

    private func autoStopMeasurement() {
        guard !hasAutoStopped else {
            return
        }
        hasAutoStopped = true
        cancelMeasurementStopTimer()
        serialReader.stop()
        stopSimulation()
        let savedFile = autosaveURL?.lastPathComponent
        closeAutosaveFile()
        connectButton.isEnabled = portPopup.isEnabled
        disconnectButton.isEnabled = false
        if let savedFile {
            setStatus("已到测试时长，CSV 已保存：\(savedFile)", connected: false)
        } else {
            setStatus("已到测试时长，自动停止", connected: false)
        }
        appendLog("已到测试时长，自动停止。")
        updateStats()
    }

    private func setStatus(_ message: String, connected: Bool) {
        statusLabel.stringValue = message
        statusLabel.textColor = connected
            ? NSColor(calibratedRed: 0.08, green: 0.48, blue: 0.45, alpha: 1)
            : .secondaryLabelColor
    }

    private func appendLog(_ line: String) {
        let stamp = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
        logView.string += "[\(stamp)] \(line)\n"
        let lines = logView.string.split(separator: "\n", omittingEmptySubsequences: false)
        if lines.count > 260 {
            logView.string = lines.suffix(220).joined(separator: "\n")
        }
        logView.scrollRangeToVisible(NSRange(location: logView.string.count, length: 0))
    }

    private func appendAutosaveSample(_ sample: RawSample) {
        do {
            try ensureAutosaveFile()
            guard let handle = autosaveHandle else {
                return
            }
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
            let parsedOhm = sample.parsedResistance.map { csvNumber($0) } ?? ""
            let parsedKohm = sample.parsedResistance.map { csvNumber($0 / 1_000) } ?? ""
            let fields = [
                String(sample.index),
                formatter.string(from: sample.receivedAt),
                csvNumber(sample.elapsed),
                parsedOhm,
                parsedKohm,
                sample.rawLine
            ]
            let row = fields.map(csvEscape).joined(separator: ",") + "\n"
            if let data = row.data(using: .utf8) {
                handle.write(data)
                handle.synchronizeFile()
            }
        } catch {
            setStatus("自动CSV写入失败：\(error.localizedDescription)", connected: false)
        }
    }

    private func ensureAutosaveFile() throws {
        if autosaveHandle != nil {
            return
        }

        let outputDirectory = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent("output", isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        let url = outputDirectory.appendingPathComponent("植物阻抗自动记录_\(fileDateStamp()).csv")
        let header = "\u{FEFF}index,received_at,elapsed_s,parsed_resistance_ohm,parsed_resistance_kohm,raw_line\n"
        try header.write(to: url, atomically: true, encoding: .utf8)
        autosaveURL = url
        autosaveHandle = try FileHandle(forWritingTo: url)
        autosaveHandle?.seekToEndOfFile()
        autosaveLabel.stringValue = "自动CSV：\(url.lastPathComponent)"
    }

    private func closeAutosaveFile() {
        autosaveHandle?.synchronizeFile()
        autosaveHandle?.closeFile()
        autosaveHandle = nil
    }

    @objc private func exportPDF() {
        guard !dataPoints.isEmpty else {
            showAlert(title: "没有可导出的数据", message: "请先连接串口并采集到电阻数据。")
            return
        }

        let panel = NSSavePanel()
        panel.title = "导出实验 PDF"
        panel.nameFieldStringValue = "植物阻抗测量报告_\(fileDateStamp()).pdf"
        panel.allowedContentTypes = [.pdf]
        panel.canCreateDirectories = true

        guard panel.runModal() == .OK, let url = panel.url else {
            return
        }

        do {
            try writePDFReport(to: url)
            setStatus("PDF 已导出：\(url.lastPathComponent)", connected: true)
        } catch {
            showAlert(title: "导出失败", message: error.localizedDescription)
        }
    }

    @objc private func exportRawCSV() {
        guard !rawSamples.isEmpty || autosaveURL != nil else {
            showAlert(title: "没有可导出的记录数据", message: "请先连接串口并记录到电阻数据。")
            return
        }

        let panel = NSSavePanel()
        panel.title = "导出记录数据 CSV"
        panel.nameFieldStringValue = "植物阻抗记录数据_\(fileDateStamp()).csv"
        if let csvType = UTType(filenameExtension: "csv") {
            panel.allowedContentTypes = [csvType]
        }
        panel.canCreateDirectories = true

        guard panel.runModal() == .OK, let url = panel.url else {
            return
        }

        do {
            try exportCompleteRawCSV(to: url)
            setStatus("记录数据已导出：\(url.lastPathComponent)", connected: true)
        } catch {
            showAlert(title: "导出失败", message: error.localizedDescription)
        }
    }

    private func exportCompleteRawCSV(to url: URL) throws {
        if let autosaveURL, FileManager.default.fileExists(atPath: autosaveURL.path) {
            autosaveHandle?.synchronizeFile()
            if autosaveURL.standardizedFileURL != url.standardizedFileURL {
                if FileManager.default.fileExists(atPath: url.path) {
                    try FileManager.default.removeItem(at: url)
                }
                try FileManager.default.copyItem(at: autosaveURL, to: url)
            }
            return
        }

        try writeRawCSV(to: url)
    }

    private func writeRawCSV(to url: URL) throws {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"

        var rows = ["index,received_at,elapsed_s,parsed_resistance_ohm,parsed_resistance_kohm,raw_line"]
        for sample in rawSamples {
            let parsedOhm = sample.parsedResistance.map { csvNumber($0) } ?? ""
            let parsedKohm = sample.parsedResistance.map { csvNumber($0 / 1_000) } ?? ""
            let fields = [
                String(sample.index),
                formatter.string(from: sample.receivedAt),
                csvNumber(sample.elapsed),
                parsedOhm,
                parsedKohm,
                sample.rawLine
            ]
            rows.append(fields.map(csvEscape).joined(separator: ","))
        }

        let csv = "\u{FEFF}" + rows.joined(separator: "\n") + "\n"
        try csv.write(to: url, atomically: true, encoding: .utf8)
    }

    private func writePDFReport(to url: URL) throws {
        var mediaBox = CGRect(x: 0, y: 0, width: 595, height: 842)
        guard let consumer = CGDataConsumer(url: url as CFURL),
              let context = CGContext(consumer: consumer, mediaBox: &mediaBox, nil) else {
            throw NSError(domain: "ResistanceGUI", code: 1, userInfo: [NSLocalizedDescriptionKey: "无法创建 PDF 文件。"])
        }

        context.beginPDFPage(nil)
        drawPDFPage(context: context, mediaBox: mediaBox)
        context.endPDFPage()
        context.closePDF()
    }

    private func drawPDFPage(context: CGContext, mediaBox: CGRect) {
        context.saveGState()
        context.translateBy(x: 0, y: mediaBox.height)
        context.scaleBy(x: 1, y: -1)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(cgContext: context, flipped: true)

        let margin: CGFloat = 42
        var y: CGFloat = 36

        drawPDFText("植物阻抗测量报告", x: margin, y: y, size: 22, weight: .bold, color: NSColor(calibratedRed: 0.06, green: 0.25, blue: 0.23, alpha: 1))
        y += 34
        drawPDFLine(x1: margin, y1: y, x2: mediaBox.width - margin, y2: y, color: NSColor(calibratedRed: 0.08, green: 0.48, blue: 0.45, alpha: 1), width: 1.4)
        y += 18

        drawPDFText("实验变量", x: margin, y: y, size: 15, weight: .bold, color: NSColor(calibratedRed: 0.08, green: 0.48, blue: 0.45, alpha: 1))
        y += 22
        for line in pdfExperimentLines() {
            drawPDFText(line, x: margin, y: y, size: 11)
            y += 18
        }

        y += 10
        drawPDFText("统计摘要", x: margin, y: y, size: 15, weight: .bold, color: NSColor(calibratedRed: 0.08, green: 0.48, blue: 0.45, alpha: 1))
        y += 22
        for line in pdfSummaryLines() {
            drawPDFText(line, x: margin, y: y, size: 11)
            y += 18
        }

        y += 16
        drawPDFText("曲线图", x: margin, y: y, size: 15, weight: .bold, color: NSColor(calibratedRed: 0.08, green: 0.48, blue: 0.45, alpha: 1))
        y += 22
        drawPDFChart(in: CGRect(x: margin, y: y, width: mediaBox.width - margin * 2, height: 300))

        NSGraphicsContext.restoreGraphicsState()
        context.restoreGState()
    }

    private func drawPDFChart(in rect: CGRect) {
        let visible = visiblePoints()
        drawPDFRect(rect, fill: NSColor(calibratedRed: 0.98, green: 0.99, blue: 1.0, alpha: 1), stroke: NSColor.separatorColor)

        let plot = rect.insetBy(dx: 46, dy: 30)
        let values = visible.map(\.value)
        let resolvedUnit = resolveUnit(for: "kohm", values: values)
        let scale = unitScale(for: resolvedUnit)
        let gridStep = customYGridStep()
        let latest = visible.last?.time ?? graphView.windowSeconds
        let minX = 0.0
        let maxX = max(graphView.windowSeconds, latest)
        var minY = values.min() ?? 0
        var maxY = values.max() ?? 1
        if let gridStep, gridStep > 0, !values.isEmpty {
            let range = pdfCustomYRange(values: values, step: gridStep)
            minY = range.minY
            maxY = range.maxY
        } else if minY == maxY {
            let pad = max(abs(minY) * 0.05, 1)
            minY -= pad
            maxY += pad
        } else {
            let pad = (maxY - minY) * 0.12
            minY -= pad
            maxY += pad
        }

        for index in 0...6 {
            let ratio = CGFloat(index) / 6
            let x = plot.minX + ratio * plot.width
            drawPDFLine(x1: x, y1: plot.minY, x2: x, y2: plot.maxY, color: NSColor(calibratedWhite: 0.72, alpha: 0.35), width: 0.6)
            let seconds = minX + Double(ratio) * (maxX - minX)
            drawPDFText(formatTimeAxisLabel(seconds), x: x - 10, y: plot.maxY + 13, size: 8, color: .secondaryLabelColor)
        }

        for value in pdfYTickValues(minY: minY, maxY: maxY, step: gridStep) {
            let ratio = CGFloat((maxY - value) / max(maxY - minY, 0.001))
            let y = plot.minY + ratio * plot.height
            drawPDFLine(x1: plot.minX, y1: y, x2: plot.maxX, y2: y, color: NSColor(calibratedWhite: 0.72, alpha: 0.35), width: 0.6)
            drawPDFText(axisLabel(value / scale.factor, step: gridStep.map { $0 / scale.factor }), x: rect.minX + 6, y: y - 6, size: 8, color: .secondaryLabelColor)
        }

        drawPDFText("电阻 \(scale.label)", x: plot.minX, y: rect.minY + 8, size: 10, weight: .bold)
        drawPDFText("时间", x: plot.maxX - 28, y: rect.maxY - 18, size: 10, weight: .bold)

        guard visible.count >= 2 else {
            drawPDFText("暂无曲线数据", x: rect.midX - 34, y: rect.midY - 8, size: 12, color: .secondaryLabelColor)
            return
        }

        let path = NSBezierPath()
        for (index, point) in visible.enumerated() {
            let x = plot.minX + CGFloat((point.time - minX) / max(maxX - minX, 0.001)) * plot.width
            let yRatio = (point.value - minY) / max(maxY - minY, 0.001)
            let y = plot.maxY - CGFloat(yRatio) * plot.height
            if index == 0 {
                path.move(to: NSPoint(x: x, y: y))
            } else {
                path.line(to: NSPoint(x: x, y: y))
            }
        }
        path.lineWidth = 2
        NSColor(calibratedRed: 0.08, green: 0.48, blue: 0.45, alpha: 1).setStroke()
        path.stroke()
    }

    private func pdfExperimentLines() -> [String] {
        let seconds = measurementDurationSeconds()
        return [
            "实验时间：\(valueOrDash(experimentTimeField.stringValue))    测试时长：\(trim(seconds / 3_600)) 小时    记录间隔：\(trim(sampleIntervalSeconds())) 秒",
            "植物编号：\(valueOrDash(plantIdField.stringValue))    电极位置：\(electrodePopup.titleOfSelectedItem ?? "--")    土壤状态：\(soilPopup.titleOfSelectedItem ?? "--")",
            "温度：\(valueOrDash(temperatureField.stringValue)) °C    湿度：\(valueOrDash(humidityField.stringValue)) %    Y格差：\(customYGridStep().map { "\(trim($0)) Ω" } ?? "自动")"
        ]
    }

    private func pdfSummaryLines() -> [String] {
        let visible = visiblePoints()
        let values = visible.map(\.rawValue)
        let average = values.isEmpty ? nil : values.reduce(0, +) / Double(values.count)
        return [
            "统计样本数：\(values.count)",
            "平均值：\(average.map { formatResistance($0, preferredUnit: "kohm") } ?? "--")",
            "最小值：\(values.min().map { formatResistance($0, preferredUnit: "kohm") } ?? "--")    最大值：\(values.max().map { formatResistance($0, preferredUnit: "kohm") } ?? "--")"
        ]
    }

    private func customYGridStep() -> Double? {
        let trimmed = yGridStepField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let step = Double(trimmed), step > 0 else {
            return nil
        }
        return step
    }

    private func pdfCustomYRange(values: [Double], step: Double) -> (minY: Double, maxY: Double) {
        let dataMin = values.min() ?? 0
        let dataMax = values.max() ?? dataMin
        let minimumSpan = step * 5
        let dataSpan = max(dataMax - dataMin, step)
        let intervalCount = max(5, ceil(dataSpan / step))
        let span = max(minimumSpan, intervalCount * step)
        let center = (dataMin + dataMax) / 2
        var minY = floor((center - span / 2) / step) * step
        var maxY = minY + span

        while dataMin < minY {
            minY -= step
            maxY -= step
        }
        while dataMax > maxY {
            minY += step
            maxY += step
        }
        return (minY, maxY)
    }

    private func pdfYTickValues(minY: Double, maxY: Double, step: Double?) -> [Double] {
        guard let step, step > 0 else {
            return (0...5).map { index in
                let ratio = Double(index) / 5
                return maxY - ratio * (maxY - minY)
            }
        }
        let intervalCount = max(1, Int(round((maxY - minY) / step)))
        let drawEvery = max(1, Int(ceil(Double(intervalCount) / 12.0)))
        let displayedStep = step * Double(drawEvery)
        let start = ceil(minY / displayedStep) * displayedStep
        var values: [Double] = []
        var value = start
        while value <= maxY + displayedStep * 0.001 {
            values.append(value)
            value += displayedStep
        }
        return values.isEmpty ? [minY, maxY] : values
    }

    private func drawPDFText(_ text: String, x: CGFloat, y: CGFloat, size: CGFloat, weight: NSFont.Weight = .regular, color: NSColor = .labelColor) {
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color
        ]
        text.draw(at: NSPoint(x: x, y: y), withAttributes: attrs)
    }

    private func drawPDFLine(x1: CGFloat, y1: CGFloat, x2: CGFloat, y2: CGFloat, color: NSColor, width: CGFloat) {
        color.setStroke()
        let path = NSBezierPath()
        path.lineWidth = width
        path.move(to: NSPoint(x: x1, y: y1))
        path.line(to: NSPoint(x: x2, y: y2))
        path.stroke()
    }

    private func drawPDFRect(_ rect: CGRect, fill: NSColor, stroke: NSColor) {
        fill.setFill()
        NSBezierPath(rect: rect).fill()
        stroke.setStroke()
        NSBezierPath(rect: rect).stroke()
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }

    private func fileDateStamp() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        return formatter.string(from: Date())
    }

    private func defaultExperimentTime() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        return formatter.string(from: Date())
    }

    private func valueOrDash(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "--" : trimmed
    }

    private func csvNumber(_ value: Double) -> String {
        String(format: "%.6f", value)
    }

    private func csvEscape(_ value: String) -> String {
        let escaped = value.replacingOccurrences(of: "\"", with: "\"\"")
        if escaped.contains(",") || escaped.contains("\"") || escaped.contains("\n") || escaped.contains("\r") {
            return "\"\(escaped)\""
        }
        return escaped
    }
}

func resolveUnit(for preferredUnit: String, values: [Double]) -> String {
    if preferredUnit != "auto" {
        return preferredUnit
    }
    let maxAbs = values.map { abs($0) }.max() ?? 0
    if maxAbs >= 1_000_000 {
        return "mohm"
    }
    if maxAbs >= 1_000 {
        return "kohm"
    }
    return "ohm"
}

func unitScale(for unit: String) -> (factor: Double, label: String) {
    switch unit {
    case "mohm":
        return (1_000_000, "MΩ")
    case "kohm":
        return (1_000, "kΩ")
    default:
        return (1, "Ω")
    }
}

func formatResistance(_ ohms: Double, preferredUnit: String) -> String {
    let unit = resolveUnit(for: preferredUnit, values: [ohms])
    let scale = unitScale(for: unit)
    return "\(trim(ohms / scale.factor)) \(scale.label)"
}

func trim(_ value: Double) -> String {
    let absolute = abs(value)
    if absolute == 0 {
        return "0"
    }
    if absolute >= 100 {
        return String(format: "%.1f", value).replacingOccurrences(of: ".0", with: "")
    }
    if absolute >= 10 {
        return String(format: "%.2f", value).trimmingTrailingZeros()
    }
    return String(format: "%.3f", value).trimmingTrailingZeros()
}

func axisLabel(_ value: Double, step: Double?) -> String {
    guard let step, step > 0 else {
        return trim(value)
    }

    let absoluteStep = abs(step)
    let decimals: Int
    if absoluteStep >= 1 {
        decimals = 1
    } else if absoluteStep >= 0.1 {
        decimals = 2
    } else if absoluteStep >= 0.01 {
        decimals = 3
    } else if absoluteStep >= 0.001 {
        decimals = 4
    } else {
        decimals = 5
    }
    return String(format: "%.\(decimals)f", value).trimmingTrailingZeros()
}

func formatTimeAxisLabel(_ seconds: Double) -> String {
    let absolute = abs(seconds)
    if absolute >= 3_600 {
        return "\(trim(seconds / 3_600))h"
    }
    if absolute >= 60 {
        return "\(trim(seconds / 60))m"
    }
    return "\(trim(seconds))s"
}

func showControlCharacters(_ value: String) -> String {
    value
        .replacingOccurrences(of: "\r", with: "\\r")
        .replacingOccurrences(of: "\n", with: "\\n")
        .replacingOccurrences(of: "\t", with: "\\t")
}

extension String {
    func trimmingTrailingZeros() -> String {
        var result = self
        while result.contains(".") && result.last == "0" {
            result.removeLast()
        }
        if result.last == "." {
            result.removeLast()
        }
        return result
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

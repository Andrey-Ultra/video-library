import AppKit
import CoreText

let root = URL(fileURLWithPath: CommandLine.arguments[1])
let iconset = root.appendingPathComponent("AppIcon.iconset")
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
let pink = NSColor(calibratedRed: 0.96, green: 0.48, blue: 0.70, alpha: 1)
let cyan = NSColor(calibratedRed: 0.39, green: 0.85, blue: 0.84, alpha: 1)
let gradient = NSGradient(starting: pink, ending: cyan)!
let background = NSColor(white: 0.11, alpha: 1)
for size in [16, 32, 128, 256, 512] {
  for scale in [1, 2] {
    let pixels = Double(size * scale)
    let canvas = NSSize(width: pixels, height: pixels)
    let rect = NSRect(origin: .zero, size: canvas)
    let image = NSImage(size: canvas)
    image.lockFocus()
    background.setFill()
    NSBezierPath(
      roundedRect: rect.insetBy(dx: pixels * 0.05, dy: pixels * 0.05),
      xRadius: pixels * 0.22, yRadius: pixels * 0.22
    ).fill()
    let border = NSBezierPath(
      roundedRect: rect.insetBy(dx: pixels * 0.16, dy: pixels * 0.16),
      xRadius: pixels * 0.15, yRadius: pixels * 0.15)
    gradient.draw(in: border, angle: -45)
    background.setFill()
    NSBezierPath(
      roundedRect: rect.insetBy(dx: pixels * 0.175, dy: pixels * 0.175),
      xRadius: pixels * 0.135, yRadius: pixels * 0.135
    ).fill()
    image.unlockFocus()

    // Apply the gradient only to the wordmark's alpha mask.
    let wordmark = NSImage(size: canvas)
    wordmark.lockFocus()
    let baseFont = NSFont.systemFont(ofSize: pixels * 0.38, weight: .semibold)
    let font =
      baseFont.fontDescriptor.withDesign(.rounded).flatMap {
        NSFont(descriptor: $0, size: baseFont.pointSize)
      } ?? baseFont
    let line = CTLineCreateWithAttributedString(
      NSAttributedString(
        string: "Vl",
        attributes: [
          .font: font, .foregroundColor: NSColor.white, .kern: pixels * 0.025,
        ]))
    let bounds = CTLineGetBoundsWithOptions(line, [.useGlyphPathBounds])
    let textRect = NSRect(
      x: (pixels - bounds.width) / 2, y: (pixels - bounds.height) / 2,
      width: bounds.width, height: bounds.height)
    let context = NSGraphicsContext.current!.cgContext
    context.textPosition = CGPoint(x: textRect.minX - bounds.minX, y: textRect.minY - bounds.minY)
    CTLineDraw(line, context)
    NSGraphicsContext.current?.compositingOperation = .sourceIn
    gradient.draw(in: textRect, angle: -45)
    wordmark.unlockFocus()
    image.lockFocus()
    wordmark.draw(in: rect)
    image.unlockFocus()
    let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
    let name = "icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
    try rep.representation(using: .png, properties: [:])!.write(
      to: iconset.appendingPathComponent(name))
  }
}
let task = Process()
task.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
task.arguments = [
  "-c", "icns", iconset.path, "-o", root.appendingPathComponent("AppIcon.icns").path,
]
try task.run()
task.waitUntilExit()
if task.terminationStatus != 0 { exit(1) }
try FileManager.default.removeItem(at: iconset)

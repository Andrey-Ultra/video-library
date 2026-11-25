import AppKit
import CoreText

enum MenuBarMark {
  static func image() -> NSImage {
    let font = NSFont.systemFont(ofSize: 14, weight: .semibold)
    let face = CTFontCreateWithName(font.fontName as CFString, font.pointSize, nil)
    let paths: [CGPath] = "Vl".utf16.compactMap { character in
      var character = character
      var glyph: CGGlyph = 0
      guard CTFontGetGlyphsForCharacters(face, &character, &glyph, 1) else { return nil }
      return CTFontCreatePathForGlyph(face, glyph, nil)
    }
    let height: CGFloat = 10
    let gap: CGFloat = 1.5
    let width =
      paths.reduce(CGFloat(0)) { result, path in
        let bounds = path.boundingBoxOfPath
        return result + bounds.width * height / bounds.height
      } + gap
    let image = NSImage(size: NSSize(width: ceil(width) + 2, height: 18), flipped: false) { rect in
      guard let context = NSGraphicsContext.current?.cgContext else { return false }
      context.setFillColor(NSColor.black.cgColor)
      var x = (rect.width - width) / 2
      for path in paths {
        let bounds = path.boundingBoxOfPath
        let scale = height / bounds.height
        // Normalize the actual outlines, so lowercase l and capital V share top and bottom.
        var transform = CGAffineTransform(
          a: scale, b: 0, c: 0, d: scale,
          tx: x - bounds.minX * scale,
          ty: (rect.height - height) / 2 - bounds.minY * scale)
        if let glyph = path.copy(using: &transform) {
          context.addPath(glyph)
          context.fillPath()
        }
        x += bounds.width * scale + gap
      }
      return true
    }
    image.isTemplate = true
    image.accessibilityDescription = "Vl"
    return image
  }
}

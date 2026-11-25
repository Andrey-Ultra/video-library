import SwiftUI

enum Palette {
  static let background = Color(red: 0.105, green: 0.108, blue: 0.12)
  static let sidebar = Color(red: 0.13, green: 0.132, blue: 0.15)
  static let panel = Color(red: 0.145, green: 0.147, blue: 0.165)
  static let line = Color.white.opacity(0.10)
  static let muted = Color(red: 0.62, green: 0.63, blue: 0.68)
  static let pink = Color(red: 0.96, green: 0.48, blue: 0.70)
  static let cyan = Color(red: 0.39, green: 0.85, blue: 0.84)
  static let gradient = LinearGradient(
    colors: [pink, cyan], startPoint: .topLeading, endPoint: .bottomTrailing)
}
struct VLMark: View {
  var size: CGFloat = 38
  var body: some View {
    Text("VL").font(.system(size: size * 0.46, weight: .semibold, design: .rounded)).tracking(-1)
      .foregroundStyle(Palette.gradient).frame(width: size, height: size)
      .background(Palette.background, in: RoundedRectangle(cornerRadius: size * 0.24))
      .overlay(RoundedRectangle(cornerRadius: size * 0.24).stroke(Palette.gradient, lineWidth: 1.2))
      .accessibilityLabel("VL")
  }
}
struct ActionButton: View {
  let label: String
  var symbol: String? = nil
  var primary = false
  var disabled = false
  let action: () -> Void
  var body: some View {
    Button(action: action) {
      HStack(spacing: 7) {
        if let symbol { Image(systemName: symbol) }
        Text(label)
      }.font(.system(size: 13, weight: .medium)).padding(.horizontal, 14).padding(.vertical, 9)
        .foregroundStyle(primary ? Palette.background : Color.white.opacity(0.9))
        .background(
          primary ? Palette.cyan : Color.white.opacity(0.055), in: RoundedRectangle(cornerRadius: 6)
        )
        .overlay(
          RoundedRectangle(cornerRadius: 6).stroke(
            primary ? Palette.cyan.opacity(0.3) : Palette.line))
    }
    .buttonStyle(.plain).disabled(disabled).opacity(disabled ? 0.4 : 1)
  }
}
struct Panel<Content: View>: View {
  @ViewBuilder let content: () -> Content
  var body: some View {
    content().background(Palette.panel, in: RoundedRectangle(cornerRadius: 9)).overlay(
      RoundedRectangle(cornerRadius: 9).stroke(Palette.line))
  }
}
struct StatusPill: View {
  let status: String
  var color: Color {
    switch status.lowercased() {
    case "complete", "indexed": Palette.cyan
    case "failed", "attention": Palette.pink
    default: Palette.muted
    }
  }
  var body: some View {
    HStack(spacing: 5) {
      Circle().fill(color).frame(width: 5, height: 5)
      Text(status.capitalized).font(.system(size: 11, weight: .medium))
    }.foregroundStyle(color)
  }
}
struct SectionCaption: View {
  let title: String
  var body: some View {
    Text(title.uppercased()).font(.system(size: 10, weight: .semibold)).tracking(1.4)
      .foregroundStyle(Palette.muted)
  }
}
struct JSONBlock: View {
  let value: JSONValue
  var body: some View {
    Text(value.pretty).font(.system(size: 12, design: .monospaced)).foregroundStyle(
      Color.white.opacity(0.75)
    ).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(14)
      .background(Palette.background, in: RoundedRectangle(cornerRadius: 6))
  }
}

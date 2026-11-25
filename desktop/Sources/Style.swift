import SwiftUI

enum Palette {
  static let background = Color(white: 0.11)
  static let sidebar = Color(white: 0.135)
  static let panel = Color(white: 0.155)
  static let line = Color.white.opacity(0.10)
  static let muted = Color.white.opacity(0.55)
  static let accent = Color(red: 0.39, green: 0.85, blue: 0.84)
  static let logoPink = Color(red: 0.96, green: 0.48, blue: 0.70)
  static let logoCyan = Color(red: 0.39, green: 0.85, blue: 0.84)
  static let gradient = LinearGradient(
    colors: [logoPink, logoCyan], startPoint: .topLeading, endPoint: .bottomTrailing)
}

struct LibraryMark: View {
  var size: CGFloat = 38
  var body: some View {
    HStack(alignment: .firstTextBaseline, spacing: size * 0.055) {
      Text("V")
      Text("l")
    }.font(.system(size: size * 0.46, weight: .semibold, design: .rounded))
      .fixedSize()
      .foregroundStyle(Palette.gradient).frame(width: size, height: size)
      .background(Palette.background, in: RoundedRectangle(cornerRadius: size * 0.24))
      .overlay(RoundedRectangle(cornerRadius: size * 0.24).stroke(Palette.gradient, lineWidth: 1.2))
      .accessibilityElement(children: .ignore)
      .accessibilityLabel("Vl")
  }
}
struct LibraryToggleStyle: ToggleStyle {
  var showsLabel = true
  func makeBody(configuration: Configuration) -> some View {
    Button {
      configuration.isOn.toggle()
    } label: {
      HStack {
        if showsLabel {
          configuration.label
          Spacer(minLength: 8)
        }
        Capsule().fill(configuration.isOn ? Palette.accent : Color.white.opacity(0.16))
          .frame(width: 28, height: 17)
          .overlay(alignment: configuration.isOn ? .trailing : .leading) {
            Circle().fill(configuration.isOn ? Palette.background : Palette.accent)
              .frame(width: 13, height: 13).padding(2)
          }
      }
    }.buttonStyle(.plain)
      .accessibilityRepresentation {
        Toggle(isOn: configuration.$isOn) { configuration.label }.toggleStyle(.switch)
      }
  }
}

struct LibraryTabs: View {
  let label: String
  let tabs: [String]
  @Binding var selection: String
  var body: some View {
    HStack(spacing: 3) {
      ForEach(tabs, id: \.self) { tab in
        Button {
          selection = tab
        } label: {
          Text(tab).font(.system(size: 12, weight: selection == tab ? .semibold : .regular))
            .frame(maxWidth: .infinity).padding(.vertical, 7)
            .foregroundStyle(selection == tab ? Color.white : Palette.muted)
            .background(
              selection == tab ? Palette.accent.opacity(0.12) : Color.clear,
              in: RoundedRectangle(cornerRadius: 5))
        }.buttonStyle(.plain)
          .accessibilityAddTraits(selection == tab ? .isSelected : [])
      }
    }.padding(3).background(Color.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 7))
      .accessibilityElement(children: .contain).accessibilityLabel(label)
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
          primary ? Palette.accent : Color.white.opacity(0.055),
          in: RoundedRectangle(cornerRadius: 6)
        )
        .overlay(
          RoundedRectangle(cornerRadius: 6).stroke(
            primary ? Palette.accent.opacity(0.3) : Palette.line))
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
    case "complete", "indexed": Palette.accent
    case "failed", "attention": Color.white.opacity(0.9)
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

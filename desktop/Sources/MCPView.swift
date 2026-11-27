import AppKit
import SwiftUI

struct MCPView: View {
  @EnvironmentObject var model: LibraryModel
  @Environment(\.dismiss) var dismiss
  @State private var format = "Codex CLI"
  @State private var copied = false
  var text: String {
    guard let state = model.state else { return "" }
    let config = state.mcp_config["mcpServers"]["video-library"]
    switch format {
    case "JSON": return state.mcp_config.pretty
    case "Codex TOML":
      return
        "[mcp_servers.video-library]\ncommand = \(config["command"].pretty)\nargs = \(config["args"].pretty)"
    default: return state.mcp_command
    }
  }
  var body: some View {
    VStack(alignment: .leading, spacing: 20) {
      HStack {
        LibraryMark()
        VStack(alignment: .leading, spacing: 7) {
          SectionCaption(title: "Model Context Protocol")
          Text("Your footage, in your agent").font(.system(size: 23, weight: .semibold))
        }
        Spacer()
      }
      Text(
        "Add this local stdio server once. Your agent can list libraries and choose a library_id independently of this window, even when the app is closed."
      ).font(.system(size: 14)).foregroundStyle(Palette.muted).lineSpacing(5)
      LibraryTabs(
        label: "Connection format", tabs: ["Codex CLI", "JSON", "Codex TOML"], selection: $format)
      ScrollView {
        Text(text).font(.system(size: 12, design: .monospaced)).textSelection(.enabled).frame(
          maxWidth: .infinity, alignment: .leading
        ).padding(17)
      }.frame(minHeight: 140, maxHeight: 260).background(
        Palette.panel, in: RoundedRectangle(cornerRadius: 7))
      Label(model.state?.active?.path ?? "Choose a folder first", systemImage: "folder").font(
        .system(size: 12)
      ).foregroundStyle(Palette.accent).textSelection(.enabled)
      Text(
        "Search · Browse events · Explore trees · Inspect frames\nAsk your agent: “What is in my active video library?”"
      ).font(.system(size: 12)).foregroundStyle(Palette.muted).lineSpacing(8)
      HStack {
        Spacer()
        ActionButton(label: "Done") { dismiss() }
        ActionButton(
          label: copied ? "Copied" : format == "Codex CLI" ? "Copy command" : "Copy configuration",
          primary: true
        ) {
          NSPasteboard.general.clearContents()
          NSPasteboard.general.setString(text, forType: .string)
          copied = true
        }
      }
    }.padding(28).frame(width: 660).background(Palette.background).preferredColorScheme(.dark)
      .onChange(of: format) { _, _ in copied = false }
  }
}

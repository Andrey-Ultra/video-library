import AppKit
import SwiftUI

struct SettingsView: View {
  @EnvironmentObject var model: LibraryModel
  @Environment(\.dismiss) var dismiss
  @State private var config: JSONValue = .object([:])
  @State private var raw = ""
  @State private var rawEdited = false
  @State private var saving = false
  @State private var error: String?
  var body: some View {
    VStack(alignment: .leading, spacing: 18) {
      HStack {
        VStack(alignment: .leading, spacing: 7) {
          SectionCaption(title: "Index settings")
          Text("Make it work for your machine").font(.system(size: 23, weight: .semibold))
        }
        Spacer()
        Button {
          dismiss()
        } label: {
          Image(systemName: "xmark")
        }.buttonStyle(.plain)
      }
      Text("Saved per library. Changed methods and their dependent layers rebuild on the next run.")
        .font(.system(size: 13)).foregroundStyle(Palette.muted)
      HStack {
        SectionCaption(title: "Preset")
        Spacer()
        ForEach(
          [("Mac · Balanced", "mac"), ("Mac · Quality", "quality"), ("NVIDIA GPU", "cuda")], id: \.1
        ) { label, key in
          ActionButton(label: label) {
            if var preset = model.state?.defaults[key] {
              preset["runtime"] = config["runtime"]
              config = preset
              raw = preset.pretty
              rawEdited = false
            }
          }
        }
      }
      ScrollView {
        VStack(alignment: .leading, spacing: 20) {
          ForEach(IndexMethod.all) { method in
            Panel {
              VStack(alignment: .leading, spacing: 13) {
                Text(method.name).font(.system(size: 15, weight: .medium))
                if method.id == "hierarchy" {
                  Toggle("Build event tree", isOn: bool("hierarchy", "enabled"))
                    .toggleStyle(LibraryToggleStyle())
                  numberRow("Windows per group", "hierarchy", "group_size")
                } else {
                  HStack {
                    Text("Method").foregroundStyle(Palette.muted)
                    Spacer()
                    Picker("Method", selection: string(method.id, "backend")) {
                      ForEach(method.backends, id: \.self) { Text(backendName($0)).tag($0) }
                    }.labelsHidden().frame(width: 230)
                  }
                  if method.id != "ocr" { textRow("Model", method.id, "model") }
                  if method.id == "visual" {
                    numberRow("Window (seconds)", "visual", "window_s")
                    numberRow("Overlap (seconds)", "visual", "overlap_s")
                    numberRow("Frames per window", "visual", "frames_per_window")
                    if config["visual"]["backend"].string == "openai" {
                      textRow("Server URL", "visual", "base_url")
                    }
                  }
                  if method.id == "ocr" { numberRow("Frames per second", "ocr", "sample_fps") }
                }
              }.font(.system(size: 13)).padding(18)
            }
          }
          DisclosureGroup("Full configuration JSON") {
            Text(
              "Includes endpoints, devices, chunk sizes and all module options. JSON edits override the form."
            ).font(.system(size: 11)).foregroundStyle(Palette.muted)
            TextEditor(
              text: Binding(
                get: { raw },
                set: {
                  raw = $0
                  rawEdited = true
                })
            ).font(.system(size: 12, design: .monospaced)).frame(height: 250).padding(8).background(
              Palette.background, in: RoundedRectangle(cornerRadius: 6))
          }.font(.system(size: 13))
        }.padding(.trailing, 5)
      }
      if let error {
        Text(error).font(.system(size: 12)).foregroundStyle(Palette.accent).textSelection(.enabled)
      }
      HStack {
        Spacer()
        ActionButton(label: "Cancel") { dismiss() }
        ActionButton(label: saving ? "Saving…" : "Save settings", primary: true, disabled: saving) {
          save()
        }
      }
    }.padding(28).frame(width: 690, height: 750).background(Palette.background)
      .preferredColorScheme(.dark)
      .onAppear {
        config = model.state?.active?.config ?? .object([:])
        raw = config.pretty
      }
  }
  func changed() { if !rawEdited { raw = config.pretty } }
  func string(_ section: String, _ key: String) -> Binding<String> {
    Binding(
      get: { config[section][key].string },
      set: { value in
        config[section][key] = .string(value)
        if section == "visual" && key == "backend" && value == "none" {
          config["hierarchy"]["enabled"] = .bool(false)
        }
        changed()
      })
  }
  func number(_ section: String, _ key: String) -> Binding<Double> {
    Binding(
      get: { config[section][key].number },
      set: {
        config[section][key] = .number($0)
        changed()
      })
  }
  func bool(_ section: String, _ key: String) -> Binding<Bool> {
    Binding(
      get: { config[section][key].bool },
      set: {
        config[section][key] = .bool($0)
        changed()
      })
  }
  func textRow(_ title: String, _ section: String, _ key: String) -> some View {
    HStack {
      Text(title).foregroundStyle(Palette.muted).frame(width: 140, alignment: .leading)
      TextField(title, text: string(section, key)).textFieldStyle(.roundedBorder).font(
        .system(size: 12)
      ).accessibilityLabel("\(section) \(title)")
    }
  }
  func numberRow(_ title: String, _ section: String, _ key: String) -> some View {
    HStack {
      Text(title).foregroundStyle(Palette.muted)
      Spacer()
      TextField(title, value: number(section, key), format: .number).textFieldStyle(.roundedBorder)
        .frame(width: 130).accessibilityLabel("\(section) \(title)")
    }
  }
  func save() {
    saving = true
    error = nil
    Task {
      do {
        let value =
          rawEdited ? try JSONDecoder().decode(JSONValue.self, from: Data(raw.utf8)) : config
        try await model.save(value)
        dismiss()
      } catch {
        self.error = error.localizedDescription
        saving = false
      }
    }
  }
  func backendName(_ name: String) -> String {
    [
      "mlx": "Apple MLX", "openai": "OpenAI-compatible server", "none": "Disabled",
      "parakeet": "Parakeet · Apple MLX", "vision": "Apple Vision", "easyocr": "EasyOCR",
      "sentence-transformers": "Sentence Transformers", "faster-whisper": "Faster Whisper",
    ][name] ?? name
  }
}
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

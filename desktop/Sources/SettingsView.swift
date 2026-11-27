import AppKit
import SwiftUI

struct SettingsView: View {
  let session: IndexSettingsSession
  @EnvironmentObject var model: LibraryModel
  @Environment(\.dismiss) var dismiss
  @State private var config: JSONValue = .object([:])
  @State private var raw = ""
  @State private var rawEdited = false
  @State private var saving = false
  @State private var error: String?
  @State private var resources: ModelResources?
  @State private var inventoryError: String?
  @State private var startingDownloads = Set<String>()
  @State private var recommendationApplied = false
  @State private var thirdPartySections = Set<String>()
  @State private var deletePrompt = false
  @State private var assetToDelete: LocalModelAsset?
  @State private var initialized = false
  @State private var inventoryRequests = LatestRequest()
  @State private var visible = false
  @State private var classifiedCatalog = false
  @State private var editedProcessingSections = Set<String>()

  var body: some View {
    VStack(alignment: .leading, spacing: 18) {
      HStack {
        VStack(alignment: .leading, spacing: 7) {
          SectionCaption(title: "Index settings")
          Text(session.initialSetup ? "How should we index this folder?" : "Change folder indexing")
            .font(.system(size: 23, weight: .semibold))
        }
        Spacer()
        Button { dismiss() } label: { Image(systemName: "xmark") }
          .buttonStyle(.plain).disabled(saving)
      }
      VStack(alignment: .leading, spacing: 5) {
        Text(session.name).font(.system(size: 14, weight: .medium))
        Text(session.path).font(.system(size: 11)).foregroundStyle(Palette.muted)
          .lineLimit(2).textSelection(.enabled)
        Text(session.initialSetup
          ? "Choose what to index. Models are downloaded separately and shared by all your libraries."
          : "Changing a layer restarts that layer and its dependent search data. Other completed work is kept.")
          .font(.system(size: 12)).foregroundStyle(Palette.muted)
      }
      ScrollView {
        VStack(alignment: .leading, spacing: 20) {
          recommendationPanel
          AnalysisProxyPanel(resolution: string("proxy", "resolution"), maxFPS: number("proxy", "max_fps"))
          analysisPanel("Visual understanding", subtitle: "Describe scenes, objects and actions.") {
            processingPicker("visual", options: [("mlx", "On this Mac"), ("none", "Off")])
            if thirdPartySections.contains("visual") {
              thirdPartyFields("visual")
            } else if config["visual"]["backend"].string == "mlx" {
              localModelPicker("visual")
            } else if config["visual"]["backend"].string == "openai" {
              textRow("Server URL", "visual", "base_url")
              textRow("Server model", "visual", "model")
              hint("Use a server with image input and an OpenAI-compatible API. Sampled frames and descriptions are sent to this address.")
              DisclosureGroup("Authentication") {
                textRow("API key variable", "visual", "api_key_env")
                hint("Set this environment variable when launching the app. Keys are not saved in the library index.")
              }
            }
            if config["visual"]["backend"].string != "none" {
              numberRow("Clip window · seconds", "visual", "window_s")
              numberRow("Images per window", "visual", "frames_per_window")
              hint("For example, 6 images from each 10-second window. More images give the model more context, but take more time and memory.")
              DisclosureGroup("Advanced sampling") {
                numberRow("Shared seconds between windows", "visual", "overlap_s")
                hint("With 10-second windows and 2 shared seconds, windows cover 0–10s, 8–18s, and so on. This helps keep actions near a boundary in context.")
                numberRow("Image size limit · pixels", "visual", "max_side")
              }
            }
          }
          analysisPanel("Speech transcription", subtitle: "Save spoken words with timestamps, in their original language.") {
            processingPicker("speech", options: [("parakeet", "On this Mac"), ("none", "Off")])
            if thirdPartySections.contains("speech") {
              thirdPartyFields("speech")
            } else if config["speech"]["backend"].string == "parakeet" {
              localModelPicker("speech")
              speechLanguages
            } else if config["speech"]["backend"].string != "none" {
              hint("This folder uses an existing custom speech setup. Choose On this Mac for the built-in Mac models, or keep it through Advanced configuration below.")
            }
          }
          analysisPanel("On-screen text", subtitle: "Recognize writing visible in the video.") {
            processingPicker("ocr", options: [("vision", "Built into macOS · Apple Vision"), ("none", "Off")])
            if config["ocr"]["backend"].string != "none" {
              numberRow("Images checked per second", "ocr", "sample_fps")
              hint("Apple Vision needs no model download. 1 image per second is a starting point for subtitles and interface text.")
            }
          }
          analysisPanel("Events & video tree", subtitle: "Group visual descriptions into events and a whole-video summary.") {
            Toggle("Build event tree", isOn: bool("hierarchy", "enabled"))
              .toggleStyle(LibraryToggleStyle()).disabled(config["visual"]["backend"].string == "none")
            if config["hierarchy"]["enabled"].bool {
              hint("Uses the same model or server as Visual understanding. No separate download.")
              DisclosureGroup("Advanced grouping") {
                numberRow("Windows per group", "hierarchy", "group_size")
              }
            } else if config["visual"]["backend"].string == "none" {
              hint("Enable Visual understanding to build an event tree.")
            }
          }
          analysisPanel("Search by meaning", subtitle: "Find moments using descriptions, even when the exact words differ.") {
            processingPicker("embedding", options: [("mlx", "On this Mac"), ("none", "Off")])
            if thirdPartySections.contains("embedding") {
              thirdPartyFields("embedding")
            } else if config["embedding"]["backend"].string == "mlx" {
              localModelPicker("embedding")
              hint("With this off, exact-word search remains available.")
            }
          }
          advancedPanel
        }.padding(.trailing, 5)
      }.disabled(saving)
      if let inventoryError {
        HStack {
          Text(inventoryError).font(.system(size: 11)).foregroundStyle(.orange)
          Spacer()
          Button("Retry") { Task { await refreshModels() } }.buttonStyle(.plain)
        }
      }
      if let error {
        Text(error).font(.system(size: 12)).foregroundStyle(.orange).textSelection(.enabled)
      }
      HStack {
        if !missingModels.isEmpty {
          Text("Download the selected models before indexing.").font(.system(size: 11)).foregroundStyle(Palette.muted)
        }
        Spacer()
        ActionButton(label: "Cancel", disabled: saving) { dismiss() }
        ActionButton(label: saving ? "Saving…" : startLabel, primary: true,
                     disabled: saving || resources == nil || !startingDownloads.isEmpty || (!rawEdited && !missingModels.isEmpty)) { save() }
      }
    }.padding(28).frame(width: 720, height: 790).background(Palette.background)
      .preferredColorScheme(.dark).interactiveDismissDisabled(saving)
      .onAppear {
        visible = true
        guard !initialized else { return }
        initialized = true
        config = session.config
        if config["speech"]["language"].string.isEmpty {
          config["speech"]["language"] = .string(config["speech"]["backend"].string == "faster-whisper" ? "en" : "auto")
        }
        if config["proxy"].object.isEmpty {
          config["proxy"] = .object(["resolution": .string("original"), "max_fps": .number(0), "cache_mb": .number(512)])
        }
        for (section, localBackend) in ModelResources.localBackends {
          if config[section]["backend"].string != "none" && config[section]["backend"].string != localBackend {
            thirdPartySections.insert(section)
          }
        }
        raw = config.pretty
      }
      .onDisappear {
        visible = false
        inventoryRequests.invalidate()
      }
      .alert("Delete downloaded model?", isPresented: $deletePrompt, presenting: assetToDelete) { asset in
        Button("Delete model", role: .destructive) { deleteModel(asset) }
        Button("Cancel", role: .cancel) {}
      } message: { asset in
        Text("\(asset.name) will be removed from the shared cache for all libraries. You can download it again. Saved indexes and source videos are kept.")
      }
      .task {
        while !Task.isCancelled {
          await refreshModels()
          do { try await Task.sleep(for: .seconds(2)) } catch { return }
        }
      }
  }
  var startLabel: String {
    model.state?.queue?.paused == true
      ? (session.initialSetup ? "Save & queue" : "Apply & queue")
      : (session.initialSetup ? "Start indexing" : "Apply & index")
  }
  var missingModels: [LocalModelAsset] {
    (resources?.models ?? []).filter { asset in
      let section = asset.section
      return config[section]["model"].string == asset.id
        && ["mlx", "parakeet"].contains(config[section]["backend"].string)
        && asset.status != "ready"
    }
  }
  var recommendationPanel: some View {
    VStack(alignment: .leading, spacing: 8) {
      HStack {
        VStack(alignment: .leading, spacing: 5) {
          Text(resources?.hardware.description ?? "Checking this Mac…").font(.system(size: 12, weight: .medium))
          hint(recommendationApplied ? "Suggested settings applied. Review them before starting."
            : "Choose a starting point based on this Mac’s chip and memory.")
        }
        Spacer()
        ActionButton(label: "Suggest for this Mac", symbol: "wand.and.stars",
                     disabled: resources?.hardware.supported != true || rawEdited) {
          guard let resources else { return }
          config = resources.suggestedConfig(preserving: config)
          thirdPartySections.removeAll()
          recommendationApplied = true
          changed()
        }
      }
    }.padding(.vertical, 4)
  }
  func analysisPanel<Content: View>(_ title: String, subtitle: String,
                                    @ViewBuilder content: @escaping () -> Content) -> some View {
    Panel {
      VStack(alignment: .leading, spacing: 13) {
        Text(title).font(.system(size: 15, weight: .medium))
        hint(subtitle)
        content()
      }.font(.system(size: 13)).padding(18)
    }
  }
  func hint(_ text: String) -> some View {
    Text(text).font(.system(size: 11)).foregroundStyle(Palette.muted)
      .fixedSize(horizontal: false, vertical: true)
  }
  func processingPicker(_ section: String, options: [(String, String)]) -> some View {
    let supportsCustom = ["visual", "speech", "embedding"].contains(section)
    let selection = Binding<String>(get: {
      thirdPartySections.contains(section) ? "third-party" : config[section]["backend"].string
    }, set: { value in
      editedProcessingSections.insert(section)
      if value == "third-party" {
        thirdPartySections.insert(section)
        if config[section]["backend"].string == "none" {
          string(section, "backend").wrappedValue = section == "speech" ? "parakeet" : "mlx"
        }
      } else {
        let wasCustom = thirdPartySections.remove(section) != nil
        string(section, "backend").wrappedValue = value
        if wasCustom && value != "none", let asset = resources?.models.first(where: { $0.section == section }) {
          config[section]["model"] = .string(asset.id)
          changed()
        }
      }
    })
    return HStack {
      Text("Process with").foregroundStyle(Palette.muted)
      Spacer()
      Picker("\(section) processing", selection: selection) {
        ForEach(options, id: \.0) { value, label in Text(label).tag(value) }
        if supportsCustom { Text("Third-party model…").tag("third-party") }
        if !supportsCustom && !options.contains(where: { $0.0 == selection.wrappedValue }) {
          Text("Existing custom setup").tag(selection.wrappedValue)
        }
      }.labelsHidden().frame(width: 320, alignment: .trailing)
    }
  }
  @ViewBuilder func thirdPartyFields(_ section: String) -> some View {
    hint("Advanced · connect a compatible model. Custom files and downloads are managed by you.")
    if section == "visual" {
      HStack {
        Text("Connection").foregroundStyle(Palette.muted)
        Spacer()
        Picker("Third-party connection", selection: string(section, "backend")) {
          Text("Local · MLX-compatible model").tag("mlx")
          Text("Server · OpenAI-compatible API").tag("openai")
        }.labelsHidden().frame(width: 320, alignment: .trailing)
      }
    }
    if config[section]["backend"].string == "openai" {
      textRow("Server URL", section, "base_url")
      textRow("Server model", section, "model")
      hint("Sampled frames and descriptions are sent to this server. The server must support images and structured JSON responses.")
      DisclosureGroup("Authentication") {
        textRow("API key variable", section, "api_key_env")
        hint("Set this environment variable when launching the app. The key is not stored in the index.")
      }
    } else {
      textRow("Model ID or path", section, "model")
      HStack {
        hint(section == "speech" ? "Use a Parakeet-compatible model for Apple Silicon." : "Use an MLX-compatible model for this task.")
        Spacer()
        ActionButton(label: "Choose folder…", symbol: "folder") { chooseModelFolder(section) }
      }
      if let asset = resources?.models.first(where: { $0.id == config[section]["model"].string }) {
        assetControls(asset)
      }
    }
  }
  func chooseModelFolder(_ section: String) {
    let panel = NSOpenPanel()
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.allowsMultipleSelection = false
    panel.prompt = "Use model"
    if panel.runModal() == .OK, let url = panel.url {
      string(section, "model").wrappedValue = url.path
    }
  }
  @ViewBuilder func localModelPicker(_ section: String) -> some View {
    let options = (resources?.models ?? []).filter { $0.section == section }
    HStack {
      Text("Model").foregroundStyle(Palette.muted)
      Spacer()
      Picker("\(section) model", selection: string(section, "model")) {
        ForEach(options) { option in Text(option.name).tag(option.id) }
        if !options.contains(where: { $0.id == config[section]["model"].string }) {
          Text(resources == nil ? "Loading models…" : "Existing custom model").tag(config[section]["model"].string)
        }
      }.labelsHidden().frame(width: 320, alignment: .trailing)
    }
    if let selected = options.first(where: { $0.id == config[section]["model"].string }) {
      hint(selected.detail)
      assetControls(selected)
    } else if resources != nil {
      hint("This is a custom model. Its installation is managed outside the built-in model list. Advanced configuration keeps its existing ID or local path.")
    }
  }
  @ViewBuilder func assetControls(_ selected: LocalModelAsset) -> some View {
    HStack(spacing: 12) {
      if selected.busy { ProgressView().controlSize(.small) }
      Text(assetLabel(selected)).font(.system(size: 12))
        .foregroundStyle(selected.status == "failed" ? Color.orange : Color.primary)
      Spacer(minLength: 8)
      Link("Details", destination: URL(string: "https://huggingface.co/\(selected.id)")!).font(.system(size: 11))
      if selected.status == "missing" || selected.status == "failed" {
        ActionButton(label: selected.status == "failed" ? "Retry download" : "Download", symbol: "arrow.down.circle",
          disabled: startingDownloads.contains(selected.id)) { download(selected) }
      }
      if selected.status == "ready" || selected.status == "failed" {
        Button { assetToDelete = selected; deletePrompt = true } label: { Image(systemName: "trash") }
          .buttonStyle(.plain).help(model.state?.queue?.running != nil ? "Pause indexing before deleting a shared model" : "Delete downloaded model from the shared cache")
          .disabled(startingDownloads.contains(selected.id) || model.state?.queue?.running != nil)
      }
    }
    if let detail = selected.error, selected.status == "failed" {
      Text(detail).font(.system(size: 11)).foregroundStyle(.orange).textSelection(.enabled)
    }
  }
  func deleteModel(_ asset: LocalModelAsset) {
    startingDownloads.insert(asset.id)
    inventoryRequests.invalidate()
    let actionConfig = config
    Task {
      defer { startingDownloads.remove(asset.id) }
      do {
        let _: JSONValue = try await model.request("api/models/delete", method: "POST",
          body: .object(["config": actionConfig, "model": .string(asset.id), "confirmed": .bool(true)]))
        await refreshModels()
      } catch { self.error = error.localizedDescription }
    }
  }
  func assetLabel(_ asset: LocalModelAsset) -> String {
    switch asset.status {
    case "ready": return "Downloaded · Ready on this Mac"
    case "checking": return "Checking downloaded files…"
    case "downloading":
      if let total = asset.total_files, total > 0 {
        return "Downloading · \(asset.completed_files ?? 0) of \(total) files"
      }
      return "Connecting to model download…"
    case "failed": return "Download needs attention"
    default: return "Not downloaded"
    }
  }
  @ViewBuilder var speechLanguages: some View {
    let option = model.state?.speech_options?.parakeet_models.first { $0.id == config["speech"]["model"].string }
    HStack {
      Text("Languages").foregroundStyle(Palette.muted)
      Spacer()
      Text(option.map { $0.languages.count == 1 ? "English only" : "Automatic · \($0.languages.count) languages" }
        ?? "Automatic · depends on model")
    }
    if let option, option.languages.count > 1 {
      hint("One pass, including English and Russian. No language selection or repeated processing needed.")
      DisclosureGroup("Supported languages") { hint(option.languages.joined(separator: ", ")) }
    }
  }
  var advancedPanel: some View {
    DisclosureGroup("Advanced configuration") {
      hint("For custom models and existing backends. JSON edits override the form; selected built-in models must still be downloaded before starting.")
      TextEditor(text: Binding(get: { raw }, set: { raw = $0; rawEdited = true }))
        .font(.system(size: 12, design: .monospaced)).frame(height: 250).padding(8)
        .background(Palette.background, in: RoundedRectangle(cornerRadius: 6))
      if rawEdited {
        Button("Use form values instead") { rawEdited = false; raw = config.pretty }
      }
    }.font(.system(size: 13))
  }
  func changed() { if !rawEdited { raw = config.pretty } }
  func string(_ section: String, _ key: String) -> Binding<String> {
    Binding(get: { config[section][key].string }, set: { value in
      let previous = config[section][key].string
      config[section][key] = .string(value)
      if key == "backend" && value != previous {
        if section == "speech" && value == "parakeet" {
          config[section]["model"] = .string("mlx-community/parakeet-tdt-0.6b-v3")
          config[section]["language"] = .string("auto")
        } else if section == "visual" && value == "mlx" {
          config[section]["model"] = .string("mlx-community/Qwen3-VL-2B-Instruct-4bit")
        } else if section == "visual" && value == "openai" {
          config[section]["model"] = .string("")
        } else if section == "embedding" && value == "mlx" {
          config[section]["model"] = .string("mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ")
        }
      }
      if section == "visual" && key == "backend" && value == "none" {
        config["hierarchy"]["enabled"] = .bool(false)
      }
      changed()
    })
  }
  func number(_ section: String, _ key: String) -> Binding<Double> {
    Binding(get: { config[section][key].number }, set: { config[section][key] = .number($0); changed() })
  }
  func bool(_ section: String, _ key: String) -> Binding<Bool> {
    Binding(get: { config[section][key].bool }, set: { config[section][key] = .bool($0); changed() })
  }
  func textRow(_ title: String, _ section: String, _ key: String) -> some View {
    HStack {
      Text(title).foregroundStyle(Palette.muted)
      Spacer()
      TextField(title, text: string(section, key)).textFieldStyle(.roundedBorder)
        .font(.system(size: 12)).frame(width: 320).accessibilityLabel("\(section) \(title)")
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
  func refreshModels() async {
    guard visible, !saving, !config.object.isEmpty else { return }
    let revision = inventoryRequests.begin()
    let requestedConfig = config
    do {
      let value: ModelResources = try await model.request("api/models/status", method: "POST",
        body: .object(["config": requestedConfig]))
      guard visible, !Task.isCancelled, inventoryRequests.accepts(revision),
        requestedConfig["runtime"] == config["runtime"] else { return }
      resources = value
      if !classifiedCatalog {
        // A delayed first inventory must not override a choice already made in the form.
        thirdPartySections.formUnion(value.customModelSections(in: config).subtracting(editedProcessingSections))
        classifiedCatalog = true
      }
      inventoryError = nil
    } catch is CancellationError {
      // Closing the sheet is not an inventory failure.
    } catch {
      guard visible, !Task.isCancelled, inventoryRequests.accepts(revision),
        requestedConfig["runtime"] == config["runtime"] else { return }
      inventoryError = error.localizedDescription
    }
  }
  func download(_ asset: LocalModelAsset) {
    startingDownloads.insert(asset.id)
    inventoryRequests.invalidate()
    let actionConfig = config
    Task {
      defer { startingDownloads.remove(asset.id) }
      do {
        let _: JSONValue = try await model.request("api/models/download", method: "POST",
          body: .object(["config": actionConfig, "model": .string(asset.id)]))
        await refreshModels()
      } catch { self.error = error.localizedDescription }
    }
  }
  func save() {
    saving = true
    inventoryRequests.invalidate()
    error = nil
    let rawToSave = rawEdited ? raw : nil
    let draftToSave = config
    Task {
      do {
        let value = try rawToSave.map { try JSONDecoder().decode(JSONValue.self, from: Data($0.utf8)) } ?? draftToSave
        if value["visual"]["backend"].string == "openai" && value["visual"]["model"].string.trimmingCharacters(in: .whitespaces).isEmpty {
          throw NSError(domain: "VideoLibrary", code: 1, userInfo: [NSLocalizedDescriptionKey: "Enter the model name provided by your API server."])
        }
        for section in ["visual", "speech", "embedding"] {
          if value[section]["backend"].string != "none" && value[section]["model"].string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw NSError(domain: "VideoLibrary", code: 1, userInfo: [NSLocalizedDescriptionKey: "Enter a model ID or choose a local model folder for \(section)."])
          }
        }
        let checked: ModelResources = try await model.request("api/models/status", method: "POST", body: .object(["config": value]))
        resources = checked
        if checked.models.contains(where: { checked.required.contains($0.id) && $0.status != "ready" }) {
          if rawToSave != nil { config = value; rawEdited = false; raw = value.pretty }
          throw NSError(domain: "VideoLibrary", code: 1, userInfo: [NSLocalizedDescriptionKey: "Download or retry the selected models above before starting indexing."])
        }
        try await model.saveIndexSettings(value, session: session)
        dismiss()
      } catch {
        self.error = error.localizedDescription
        saving = false
      }
    }
  }
}

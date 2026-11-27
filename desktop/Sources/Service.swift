import AppKit
import Foundation
import SwiftUI

@MainActor final class LibraryModel: ObservableObject {
  @Published var state: Snapshot?
  @Published var error: String?
  @Published var baseURL: URL?
  @Published var selectedVideo: Int?
  @Published var indexSettings: IndexSettingsSession?
  @Published var mcpShown = false
  @Published var refreshing = false
  @Published var queueExpanded = false
  var hideToMenuBar: () -> Void = {}
  private var service: Process?
  private var monitor: Task<Void, Never>?
  private var latestReload = LatestRequest()
  private var errorLog: FileHandle?
  private var clipWindows: [ClipRoute: ClipWindowController] = [:]
  private var reportedJobFailures = Set<String>()
  let project: URL
  let stateDirectory: URL
  init() {
    let fallback = Bundle.main.bundleURL.deletingLastPathComponent()
    project = URL(
      fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "VideoAgentProjectRoot") as? String
        ?? fallback.path)
    if let i = CommandLine.arguments.firstIndex(of: "--state-dir"),
      CommandLine.arguments.count > i + 1
    {
      stateDirectory = URL(fileURLWithPath: CommandLine.arguments[i + 1])
    } else {
      stateDirectory = project.appendingPathComponent(".cache/app")
    }
  }
  func start() {
    guard service == nil else { return }
    do {
      try FileManager.default.createDirectory(at: stateDirectory, withIntermediateDirectories: true)
      let log = stateDirectory.appendingPathComponent("desktop-service.log")
      FileManager.default.createFile(atPath: log.path, contents: nil)
      errorLog = try FileHandle(forWritingTo: log)
      let process = Process()
      let pipe = Pipe()
      process.executableURL = project.appendingPathComponent(".venv/bin/python")
      process.currentDirectoryURL = project
      process.arguments = [
        "-m", "video_agent.desktop_service", "--state-dir", stateDirectory.path, "--parent-pid",
        String(ProcessInfo.processInfo.processIdentifier),
      ]
      process.standardOutput = pipe
      process.standardError = errorLog
      try process.run()
      service = process
      let handle = pipe.fileHandleForReading
      Task {
        do {
          let line: Data = try await Task.detached {
            var result = Data()
            while let next = try handle.read(upToCount: 1), !next.isEmpty {
              if next == Data([10]) { break }
              result.append(next)
              if result.count > 8192 { throw CocoaError(.fileReadCorruptFile) }
            }
            return result
          }.value
          let value = try JSONDecoder().decode([String: String].self, from: line)
          guard let address = value["url"], let url = URL(string: address) else {
            throw CocoaError(.fileReadCorruptFile)
          }
          baseURL = url
          for _ in 0..<60 {
            do {
              try await reload()
              break
            } catch { try await Task.sleep(for: .milliseconds(100)) }
          }
          guard state != nil else { throw URLError(.cannotConnectToHost) }
          monitor = Task {
            while !Task.isCancelled {
              do {
                try await Task.sleep(for: .seconds(2))
                try await reload()
              } catch is CancellationError { break } catch {
                self.error = "Local service is unavailable. Reopen the app to reconnect."
              }
            }
          }
        } catch {
          self.error =
            "Could not start the local service: \(error.localizedDescription). See .cache/app/desktop-service.log."
        }
      }
    } catch { self.error = error.localizedDescription }
  }
  func stop() {
    monitor?.cancel()
    latestReload.invalidate()
    if let process = service, process.isRunning { process.terminate() }
    service = nil
    try? errorLog?.close()
  }
  func pausePreviews() {
    for controller in clipWindows.values { controller.pausePlayback?() }
  }
  func openClip(_ route: ClipRoute, title: String) {
    if let controller = clipWindows[route] {
      controller.showWindow(nil)
      controller.window?.makeKeyAndOrderFront(nil)
      return
    }
    let clipModel = ClipModel(route: route)
    let view = ClipDetailView(clip: clipModel).environmentObject(self)
    let window = NSWindow(contentViewController: NSHostingController(rootView: view))
    window.styleMask = [.titled, .closable, .resizable, .miniaturizable]
    window.title = title + " — Clip details"
    window.appearance = NSAppearance(named: .darkAqua)
    window.setContentSize(NSSize(width: 1220, height: 820))
    window.minSize = NSSize(width: 1000, height: 700)
    window.center()
    let controller = ClipWindowController(window: window)
    controller.pausePlayback = { [weak clipModel] in clipModel?.playback.pause() }
    controller.onClose = { [weak self] in
      clipModel.close()
      self?.clipWindows.removeValue(forKey: route)
    }
    clipWindows[route] = controller
    controller.showWindow(nil)
    window.makeKeyAndOrderFront(nil)
  }
  func request<T: Decodable>(_ path: String, method: String = "GET", body: JSONValue? = nil)
    async throws -> T
  {
    guard let url = baseURL?.appendingPathComponent(path) else {
      throw URLError(.cannotConnectToHost)
    }
    var request = URLRequest(url: url)
    request.httpMethod = method
    request.timeoutInterval = 45
    request.setValue("local", forHTTPHeaderField: "X-Video-Agent")
    if let body {
      request.httpBody = try JSONEncoder().encode(body)
      request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    }
    let (data, response) = try await URLSession.shared.data(for: request)
    guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
      let value = try? JSONDecoder().decode(JSONValue.self, from: data)
      throw NSError(
        domain: "VideoLibrary", code: 1,
        userInfo: [
          NSLocalizedDescriptionKey: value?["detail"].string ?? "Local service request failed"
        ])
    }
    return try JSONDecoder().decode(T.self, from: data)
  }
  func query<T: Decodable>(_ path: String, parameters: [String: String]) async throws -> T {
    guard let base = baseURL?.appendingPathComponent(path) else {
      throw URLError(.cannotConnectToHost)
    }
    var parts = URLComponents(url: base, resolvingAgainstBaseURL: false)!
    parts.queryItems = parameters.map { URLQueryItem(name: $0.key, value: $0.value) }
    let (data, response) = try await URLSession.shared.data(from: parts.url!)
    guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
      let error = try? JSONDecoder().decode(JSONValue.self, from: data)
      throw NSError(
        domain: "VideoLibrary", code: 1,
        userInfo: [NSLocalizedDescriptionKey: error?["detail"].string ?? "Could not load evidence"])
    }
    return try JSONDecoder().decode(T.self, from: data)
  }
  func reload() async throws {
    let revision = latestReload.begin()
    let value: Snapshot = try await request("api/state")
    guard latestReload.accepts(revision), !Task.isCancelled else { return }
    if state != value { state = value }
    if error == nil, let job = value.job, job.status == "failed",
      let detail = job.error, !detail.isEmpty, !reportedJobFailures.contains(job.id)
    {
      reportedJobFailures.insert(job.id)
      error = detail
    }
  }
  func perform(_ action: @escaping @MainActor () async throws -> Void) {
    Task {
      do {
        try await action()
        try await reload()
      } catch { self.error = error.localizedDescription }
    }
  }
  func selectFolder(_ path: String, scan: Bool = false) {
    selectedVideo = nil
    perform {
      let lib: Library = try await self.request(
        "api/libraries", method: "POST", body: .object(["path": .string(path)]))
      if scan {
        let _: JSONValue = try await self.request(
          "api/libraries/\(lib.id)/jobs", method: "POST",
          body: .object(["kind": .string("refresh")]))
      }
    }
  }
  func addFolder(_ path: String) {
    perform {
      let inspected: FolderInspection = try await self.request(
        "api/libraries/inspect", method: "POST", body: .object(["path": .string(path)]))
      if inspected.has_saved_config {
        let library: Library = try await self.request(
          "api/libraries", method: "POST", body: .object(["path": .string(inspected.path)]))
        self.selectedVideo = nil
        if inspected.needs_resume ?? true {
          guard try await self.ensureIndexModels(library) else { return }
          let _: JSONValue = try await self.request(
            "api/libraries/\(library.id)/jobs", method: "POST", body: .object(["kind": .string("index")]))
        }
      } else {
        self.indexSettings = IndexSettingsSession(path: inspected.path, libraryID: inspected.library_id,
          config: inspected.config, initialSetup: true)
      }
    }
  }
  func editIndexSettings(_ library: Library) {
    indexSettings = IndexSettingsSession(path: library.path, libraryID: library.id,
      config: library.config, initialSetup: false)
  }
  func confirmDeleteIndex(_ library: Library) {
    confirmDeleteLibrary(library, indexOnly: true)
  }
  func confirmDeleteLibrary(_ library: Library, indexOnly: Bool = false) {
    let alert = NSAlert()
    alert.messageText = indexOnly ? "Delete index for “\(library.name)”?"
      : "Delete library “\(library.name)” and its index?"
    alert.informativeText = indexOnly
      ? "This deletes all saved indexing data and removes waiting tasks for this folder. The folder stays in Libraries and its indexing setup opens next. Original videos stay unchanged."
      : "This removes the folder from Libraries and deletes its saved index, including descriptions, transcripts and search data. Waiting tasks for this folder will be removed. Original videos stay unchanged."
    alert.alertStyle = .warning
    alert.addButton(withTitle: indexOnly ? "Delete index" : "Delete library and index")
    alert.addButton(withTitle: "Cancel")
    alert.buttons.first?.hasDestructiveAction = true
    alert.buttons.first?.keyEquivalent = ""
    alert.buttons.last?.keyEquivalent = "\r"
    if let window = NSApp.keyWindow {
      alert.beginSheetModal(for: window) { [weak self] response in
        if response == .alertFirstButtonReturn {
          if indexOnly { self?.deleteIndex(library) } else { self?.deleteLibrary(library) }
        }
      }
    } else if alert.runModal() == .alertFirstButtonReturn {
      if indexOnly { deleteIndex(library) } else { deleteLibrary(library) }
    }
  }
  private func deleteIndex(_ library: Library) {
    perform {
      let result: IndexResetResult = try await self.request(
        "api/libraries/\(library.id)/index", method: "DELETE",
        body: .object(["confirmed": .bool(true)]))
      for route in self.clipWindows.keys.filter({ $0.libraryID == library.id }) {
        self.clipWindows[route]?.pausePlayback?()
        self.clipWindows[route]?.close()
      }
      self.selectedVideo = nil
      try await self.reload()
      self.indexSettings = IndexSettingsSession(path: result.library.path,
        libraryID: result.library.id, config: result.library.config, initialSetup: true)
      if !result.warnings.isEmpty { self.error = result.warnings.joined(separator: "\n") }
    }
  }
  private func deleteLibrary(_ library: Library) {
    perform {
      let result: JSONValue = try await self.request(
        "api/libraries/\(library.id)", method: "DELETE",
        body: .object(["confirmed": .bool(true)]))
      for route in self.clipWindows.keys.filter({ $0.libraryID == library.id }) {
        self.clipWindows[route]?.pausePlayback?()
        self.clipWindows[route]?.close()
      }
      if library.id == self.state?.active_id { self.selectedVideo = nil }
      if case .array(let warnings) = result["warnings"], !warnings.isEmpty {
        self.error = warnings.map { $0.string }.joined(separator: "\n")
      }
    }
  }
  func chooseFolder() {
    let panel = NSOpenPanel()
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.allowsMultipleSelection = false
    panel.message = "Choose a folder containing your videos"
    panel.prompt = "Open library"
    panel.begin { [weak self] result in
      if result == .OK, let url = panel.url {
        Task { @MainActor in self?.addFolder(url.path) }
      }
    }
  }
  func ensureIndexModels(_ library: Library) async throws -> Bool {
    let assets: ModelResources = try await request("api/models/status", method: "POST",
      body: .object(["config": library.config]))
    if assets.models.contains(where: { assets.required.contains($0.id) && $0.status != "ready" }) {
      editIndexSettings(library)
      return false
    }
    return true
  }
  func run(_ kind: String = "index") {
    guard let library = state?.active else { return }
    let id = library.id
    perform {
      if kind == "index" {
        guard try await self.ensureIndexModels(library) else { return }
      }
      let _: JSONValue = try await self.request(
        "api/libraries/\(id)/jobs", method: "POST", body: .object(["kind": .string(kind)]))
    }
  }
  func pause() {
    queueControl("pause")
  }
  func queueControl(_ action: String) {
    perform {
      let _: JSONValue = try await self.request(
        "api/queue/\(action)", method: "POST", body: .object([:]))
    }
  }
  func queueAction(_ task: QueueTask, _ action: String, direction: String? = nil) {
    perform {
      let _: JSONValue = try await self.request(
        "api/jobs/\(task.id)/\(action)", method: "POST",
        body: .object(direction.map { ["direction": .string($0)] } ?? [:]))
    }
  }
  func queuedTask(_ libraryID: String) -> QueueTask? {
    state?.queue?.jobs.first { $0.library_id == libraryID && ($0.running || $0.waiting) }
  }
  func saveIndexSettings(_ config: JSONValue, session: IndexSettingsSession) async throws {
    let id: String
    if let libraryID = session.libraryID {
      let _: JSONValue = try await request("api/libraries/\(libraryID)/config", method: "PUT", body: config)
      id = libraryID
    } else {
      let library: Library = try await request("api/libraries/configure", method: "POST",
        body: .object(["path": .string(session.path), "config": config]))
      id = library.id
    }
    if session.initialSetup && session.libraryID != nil {
      let _: Library = try await request("api/libraries", method: "POST",
        body: .object(["path": .string(session.path)]))
    }
    if config != session.config {
      for route in clipWindows.keys.filter({ $0.libraryID == id }) {
        clipWindows[route]?.pausePlayback?()
        clipWindows[route]?.close()
      }
      if state?.active_id == id { selectedVideo = nil }
    }
    let _: JSONValue = try await request("api/libraries/\(id)/jobs", method: "POST",
      body: .object(["kind": .string("index")]))
    try await reload()
  }
  func thumbnail(_ video: Video, libraryID: String? = nil) -> URL? {
    guard let baseURL else { return nil }
    var components = URLComponents(
      url: baseURL.appendingPathComponent("api/videos/\(video.id)/thumbnail"),
      resolvingAgainstBaseURL: false)!
    components.queryItems = [
      URLQueryItem(name: "library_id", value: libraryID ?? state?.active_id),
      URLQueryItem(name: "identity", value: video.fingerprint),
    ]
    return components.url
  }
}

@MainActor final class ClipWindowController: NSWindowController, NSWindowDelegate {
  var onClose: (() -> Void)?
  var pausePlayback: (() -> Void)?
  override init(window: NSWindow?) {
    super.init(window: window)
    window?.delegate = self
  }
  required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }
  func windowWillClose(_ notification: Notification) { onClose?() }
}

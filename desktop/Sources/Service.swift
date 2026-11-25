import AppKit
import Foundation
import SwiftUI

@MainActor final class LibraryModel: ObservableObject {
  @Published var state: Snapshot?
  @Published var error: String?
  @Published var baseURL: URL?
  @Published var selectedVideo: Int?
  @Published var settingsShown = false
  @Published var mcpShown = false
  @Published var refreshing = false
  @Published var queueExpanded = false
  var hideToMenuBar: () -> Void = {}
  private var service: Process?
  private var monitor: Task<Void, Never>?
  private var errorLog: FileHandle?
  private var clipWindows: [ClipRoute: ClipWindowController] = [:]
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
      clipModel.playback.close()
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
    let value: Snapshot = try await request("api/state")
    if state != value { state = value }
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
  func chooseFolder() {
    let panel = NSOpenPanel()
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.allowsMultipleSelection = false
    panel.message = "Choose a folder containing your videos"
    panel.prompt = "Open library"
    panel.begin { [weak self] result in
      if result == .OK, let url = panel.url {
        Task { @MainActor in self?.selectFolder(url.path, scan: true) }
      }
    }
  }
  func run(_ kind: String = "index") {
    guard let id = state?.active_id else { return }
    perform {
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
  func save(_ config: JSONValue) async throws {
    guard let id = state?.active_id else { return }
    let _: JSONValue = try await request("api/libraries/\(id)/config", method: "PUT", body: config)
    try await reload()
  }
  func toggle(_ method: IndexMethod, _ enabled: Bool) {
    guard var config = state?.active?.config else { return }
    if method.id == "hierarchy" {
      config["hierarchy"]["enabled"] = .bool(enabled)
    } else {
      let preference = "\(state?.active_id ?? "")-\(method.id)-backend"
      if enabled {
        config[method.id]["backend"] = .string(
          UserDefaults.standard.string(forKey: preference) ?? method.backends.first ?? "none")
      } else {
        UserDefaults.standard.set(config[method.id]["backend"].string, forKey: preference)
        config[method.id]["backend"] = .string("none")
      }
      if method.id == "visual" && !enabled { config["hierarchy"]["enabled"] = .bool(false) }
    }
    let saved = config
    perform { try await self.save(saved) }
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

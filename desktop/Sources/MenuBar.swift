import AppKit
import Combine

/// Owns the status item independently of the lifetime of any application window.
@MainActor final class MenuBarController: NSObject, NSMenuDelegate {
  private let model: LibraryModel
  private weak var appDelegate: AppDelegate?
  private let statusItem: NSStatusItem
  private let menu = NSMenu()
  private let libraryItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
  private let stateItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
  private let videosItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
  private let indexedItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
  private let progressItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
  private let indexingItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
  private let hideItem = NSMenuItem(title: "Hide to Menu Bar", action: nil, keyEquivalent: "")
  private var subscription: AnyCancellable?

  init(model: LibraryModel, delegate: AppDelegate) {
    self.model = model
    self.appDelegate = delegate
    statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    super.init()
    if let button = statusItem.button {
      button.title = ""
      button.image = MenuBarMark.image()
      button.imagePosition = .imageOnly
      button.setAccessibilityLabel("Video library menu")
    }
    menu.autoenablesItems = false
    menu.delegate = self
    let heading = NSMenuItem(title: "Video library", action: nil, keyEquivalent: "")
    heading.isEnabled = false
    libraryItem.isEnabled = false
    stateItem.isEnabled = false
    videosItem.isEnabled = false
    indexedItem.isEnabled = false
    progressItem.isEnabled = false
    menu.addItem(heading)
    menu.addItem(libraryItem)
    menu.addItem(stateItem)
    menu.addItem(videosItem)
    menu.addItem(indexedItem)
    menu.addItem(progressItem)
    menu.addItem(.separator())
    let showItem = NSMenuItem(
      title: "Show Video library", action: #selector(showLibrary), keyEquivalent: "")
    showItem.target = self
    menu.addItem(showItem)
    hideItem.action = #selector(hideLibrary)
    hideItem.target = self
    menu.addItem(hideItem)
    let queueItem = NSMenuItem(title: "Show queue", action: #selector(showQueue), keyEquivalent: "")
    queueItem.target = self
    menu.addItem(queueItem)
    menu.addItem(.separator())
    indexingItem.action = #selector(controlIndexing)
    indexingItem.target = self
    menu.addItem(indexingItem)
    menu.addItem(.separator())
    let quitItem = NSMenuItem(
      title: "Quit Video library", action: #selector(quit), keyEquivalent: "q")
    quitItem.target = self
    menu.addItem(quitItem)
    statusItem.menu = menu
    subscription = model.$state.sink { [weak self] snapshot in self?.update(snapshot) }
  }

  func menuNeedsUpdate(_ menu: NSMenu) { update(model.state) }

  private func update(_ state: Snapshot?) {
    hideItem.isEnabled = NSApp.activationPolicy() == .regular
    let queue = state?.queue
    let task = queue?.running
    libraryItem.title =
      task.map { "Processing: " + $0.folder }
      ?? (queue?.paused == true ? "Queue paused" : "Idle")
    libraryItem.toolTip = task?.folder
    stateItem.title =
      task.map { task in
        task.status == "pausing"
          ? "Saving checkpoints…"
          : "Stage: "
            + (IndexMethod.all.first { $0.id == task.current_stage }?.name
              ?? (task.kind == "refresh" ? "Scanning folder" : "Preparing task…"))
      } ?? ((queue?.pendingCount ?? 0) > 0 ? "Tasks saved · waiting" : "Queue empty")
    videosItem.title = "Waiting: \(queue?.waiting_count ?? 0) tasks"
    indexedItem.title =
      task.map { "Indexed: \($0.indexed_count ?? 0) of \($0.video_count ?? 0) videos" }
      ?? "Pending: \(queue?.pendingCount ?? 0) tasks"
    progressItem.title =
      task?.progress.map { progress in
        "Progress: \(Int(progress.fraction * 100))%"
          + (progress.eta_s.map { " · ~" + duration($0) + " remaining" } ?? " · ETA calibrating")
      } ?? ""
    progressItem.isHidden = task == nil
    indexingItem.title = queue?.paused == true ? "Resume queue" : "Pause queue"
    indexingItem.isEnabled = (queue?.pendingCount ?? 0) > 0 && task?.status != "pausing"
    statusItem.button?.toolTip = "Video library · " + libraryItem.title + " · " + stateItem.title
  }

  @objc private func showLibrary() { appDelegate?.showLibrary() }
  @objc private func showQueue() {
    model.queueExpanded = true
    appDelegate?.showLibrary()
  }
  @objc private func hideLibrary() { appDelegate?.hideToMenuBar() }
  @objc private func controlIndexing() {
    model.queueControl(model.state?.queue?.paused == true ? "resume" : "pause")
  }
  @objc private func quit() { NSApp.terminate(nil) }

  func remove() {
    subscription?.cancel()
    subscription = nil
    NSStatusBar.system.removeStatusItem(statusItem)
  }
}

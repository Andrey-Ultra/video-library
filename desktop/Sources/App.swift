import AppKit
import SwiftUI

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
  var model: LibraryModel?
  weak var mainWindow: NSWindow?
  private var statusController: MenuBarController?
  private var openLibraryWindow: (() -> Void)?
  private var closingObserver: NSObjectProtocol?
  private var isQuitting = false

  func attach(model: LibraryModel, openLibraryWindow: @escaping () -> Void) {
    self.model = model
    self.openLibraryWindow = openLibraryWindow
    model.hideToMenuBar = { [weak self] in self?.hideToMenuBar() }
    if statusController == nil {
      statusController = MenuBarController(model: model, delegate: self)
    }
    if closingObserver == nil {
      closingObserver = NotificationCenter.default.addObserver(
        forName: NSWindow.willCloseNotification, object: nil, queue: .main
      ) { [weak self] notification in
        MainActor.assumeIsolated {
          guard let self, let window = notification.object as? NSWindow else { return }
          if window === self.mainWindow { self.mainWindow = nil }
        }
      }
    }
  }

  func showLibrary() {
    NSApp.setActivationPolicy(.regular)
    if let mainWindow {
      mainWindow.deminiaturize(nil)
      mainWindow.makeKeyAndOrderFront(nil)
    } else {
      openLibraryWindow?()
    }
    NSApp.unhide(nil)
    NSApp.activate(ignoringOtherApps: true)
  }

  func hideToMenuBar() {
    model?.pausePreviews()
    NSApp.hide(nil)
    NSApp.setActivationPolicy(.accessory)
  }

  func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
    if !isQuitting {
      DispatchQueue.main.async { [weak self] in self?.hideToMenuBar() }
    }
    return false
  }

  func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool
  {
    showLibrary()
    return false
  }

  func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    isQuitting = true
    return .terminateNow
  }

  func applicationWillTerminate(_ notification: Notification) {
    isQuitting = true
    model?.pausePreviews()
    model?.stop()
    statusController?.remove()
    if let closingObserver { NotificationCenter.default.removeObserver(closingObserver) }
  }
  func applicationDidFinishLaunching(_ notification: Notification) {
    NSApp.activate(ignoringOtherApps: true)
  }
}
@main struct VideoLibraryApp: App {
  @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
  @StateObject private var model = LibraryModel()
  var body: some Scene {
    WindowGroup("Video library", id: "library") {
      LibraryScene(delegate: delegate).environmentObject(model)
    }
    .defaultSize(width: 1340, height: 880).windowStyle(.titleBar)
    .commands {
      CommandGroup(replacing: .newItem) {
        Button("Open video folder…", action: model.chooseFolder).keyboardShortcut("o")
      }
      CommandGroup(after: .toolbar) { Button("Connect your agent…") { model.mcpShown = true } }
      CommandGroup(after: .appVisibility) {
        Button("Hide to Menu Bar") { delegate.hideToMenuBar() }
          .keyboardShortcut("m", modifiers: [.command, .shift])
      }
    }
  }
}

struct LibraryScene: View {
  @EnvironmentObject private var model: LibraryModel
  @Environment(\.openWindow) private var openWindow
  let delegate: AppDelegate
  var body: some View {
    MainView()
      .background(MainWindowCapture { delegate.mainWindow = $0 })
      .onAppear {
        delegate.attach(model: model, openLibraryWindow: { openWindow(id: "library") })
      }
  }
}

private struct MainWindowCapture: NSViewRepresentable {
  let attached: (NSWindow) -> Void
  func makeNSView(context: Context) -> WindowCaptureView {
    let view = WindowCaptureView()
    view.attached = attached
    return view
  }
  func updateNSView(_ view: WindowCaptureView, context: Context) { view.attached = attached }
}

private final class WindowCaptureView: NSView {
  var attached: ((NSWindow) -> Void)?
  override func viewDidMoveToWindow() {
    super.viewDidMoveToWindow()
    if let window { attached?(window) }
  }
}

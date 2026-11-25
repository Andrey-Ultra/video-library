import AppKit
import SwiftUI

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
  var model: LibraryModel?
  func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
  func applicationWillTerminate(_ notification: Notification) { model?.stop() }
  func applicationDidFinishLaunching(_ notification: Notification) {
    NSApp.activate(ignoringOtherApps: true)
  }
}
@main struct VideoLibraryApp: App {
  @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
  @StateObject private var model = LibraryModel()
  var body: some Scene {
    WindowGroup("Video Library", id: "library") {
      MainView().environmentObject(model).onAppear { delegate.model = model }
    }
    .defaultSize(width: 1340, height: 880).windowStyle(.titleBar)
    .commands {
      CommandGroup(replacing: .newItem) {
        Button("Open video folder…", action: model.chooseFolder).keyboardShortcut("o")
        Button("Index settings…") { model.settingsShown = true }.keyboardShortcut(",")
      }
      CommandGroup(after: .toolbar) { Button("Connect your agent…") { model.mcpShown = true } }
    }
  }
}

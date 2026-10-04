import AppKit
import SwiftUI

/// Keeps the app out of the Dock when it runs outside its bundle, as with `swift run`.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
    }
}

/// CimriHook in the menu bar: the subscription windows of Claude Code and Codex and the gain
/// since the last `cimrihook init`, read from the `cimrihook` CLI.
@main
struct CimriHookBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var store = PanelStore()

    var body: some Scene {
        MenuBarExtra {
            PanelView(store: store)
        } label: {
            Image(nsImage: menuBarImage(store.claude.value, store.codex.value))
        }
        .menuBarExtraStyle(.window)
    }
}

import AppKit
import BridgeShared

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var server: BridgeServer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            server = try BridgeServer()
            try server?.start()
        } catch {
            NSLog("Hunch Safari bridge failed to start: %@", error.localizedDescription)
            NSApplication.shared.terminate(nil)
        }
    }
}

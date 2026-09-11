import BridgeShared
import Foundation
import Network
import SafariServices

@objc(SafariWebExtensionHandler)
final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    private static let lock = NSLock()
    private let queue = DispatchQueue(label: "com.tryhunch.safari.extension-receipt")

    private static func pendingURL() throws -> URL {
        try BridgePaths.stateDirectory().appendingPathComponent("pending-commands.json")
    }

    func beginRequest(with context: NSExtensionContext) {
        let rawInfo = (context.inputItems.first as? NSExtensionItem)?.userInfo
        if let url = try? BridgePaths.stateDirectory().appendingPathComponent("handler.log") {
            let line = "\(Date()) keys=\(rawInfo?.keys.map { String(describing: $0) } ?? [])\n"
            if let handle = try? FileHandle(forWritingTo: url) {
                handle.seekToEndOfFile()
                handle.write(Data(line.utf8))
                try? handle.close()
            } else {
                try? Data(line.utf8).write(to: url)
            }
        }

        guard let item = context.inputItems.first as? NSExtensionItem else {
            finish(context, ["accepted": true])
            return
        }
        let rawMessage = item.userInfo?[SFExtensionMessageKey] as? [String: Any]
            ?? item.userInfo as? [String: Any]
            ?? [:]
        let message = unwrap(rawMessage)

        if message["kind"] as? String == "pull" {
            finish(context, dequeue() ?? ["kind": "idle"])
            return
        }

        if isReceipt(message) {
            sendReceipt(message) { [weak self] in
                self?.finish(context, ["accepted": true])
            }
            return
        }

        // App-to-extension dispatchMessage is delivered here when the JS native
        // port is not yet connected. Persist it so a later pull from background.js
        // can run even if Safari launched a fresh extension process.
        if message["operation"] != nil {
            enqueue(message)
            if message["operation"] as? String == "status" {
                var receipt = message
                receipt["kind"] = "extension_receipt"
                receipt["status"] = "verified"
                receipt["extensionEnabled"] = true
                receipt["tabs"] = []
                receipt["reason"] = "native handler alive; JavaScript tabs pending"
                sendReceipt(receipt) { [weak self] in
                    self?.finish(context, ["queued": true])
                }
                return
            }
            finish(context, ["queued": true])
            return
        }

        finish(context, ["accepted": true])
    }

    private func unwrap(_ message: [String: Any]) -> [String: Any] {
        guard let inner = message["userInfo"] as? [String: Any] else { return message }
        var merged = inner
        if message["name"] != nil { merged["name"] = message["name"] }
        return merged
    }

    private func isReceipt(_ message: [String: Any]) -> Bool {
        if message["kind"] as? String == "extension_receipt" { return true }
        return message["receiptNonce"] != nil && message["status"] != nil && message["operation"] == nil
    }

    private func enqueue(_ message: [String: Any]) {
        Self.lock.lock()
        defer { Self.lock.unlock() }
        var items = loadPending()
        items.append(message)
        if items.count > 32 { items.removeFirst(items.count - 32) }
        savePending(items)
    }

    private func dequeue() -> [String: Any]? {
        Self.lock.lock()
        defer { Self.lock.unlock() }
        var items = loadPending()
        guard !items.isEmpty else { return nil }
        let first = items.removeFirst()
        savePending(items)
        return first
    }

    private func loadPending() -> [[String: Any]] {
        guard let url = try? Self.pendingURL(),
              let data = try? Data(contentsOf: url),
              let raw = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
            return []
        }
        return raw
    }

    private func savePending(_ items: [[String: Any]]) {
        guard let url = try? Self.pendingURL() else { return }
        if items.isEmpty {
            try? FileManager.default.removeItem(at: url)
            return
        }
        guard JSONSerialization.isValidJSONObject(items),
              let data = try? JSONSerialization.data(withJSONObject: items) else { return }
        try? data.write(to: url, options: .atomic)
    }

    private func sendReceipt(_ message: [String: Any], done: @escaping () -> Void) {
        guard message["protocol"] as? Int == BridgePaths.protocolVersion,
              let identifier = message["id"] as? String,
              UUID(uuidString: identifier) != nil,
              let nonce = message["receiptNonce"] as? String,
              UUID(uuidString: nonce) != nil,
              let rawPort = message["receiptPort"] as? Int,
              let portValue = UInt16(exactly: rawPort),
              let port = NWEndpoint.Port(rawValue: portValue),
              JSONSerialization.isValidJSONObject(message) else {
            done()
            return
        }
        var receipt = message
        receipt["kind"] = "extension_receipt"
        guard JSONSerialization.isValidJSONObject(receipt),
              let data = try? JSONSerialization.data(withJSONObject: receipt) else {
            done()
            return
        }
        if let url = try? BridgePaths.receiptURL(id: identifier) {
            try? data.write(to: url, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        }

        let connection = NWConnection(host: "127.0.0.1", port: port, using: .tcp)
        var finished = false
        func finishOnce() {
            guard !finished else { return }
            finished = true
            connection.cancel()
            done()
        }
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                connection.send(content: data + Data([0x0A]), completion: .contentProcessed { _ in
                    finishOnce()
                })
            case .failed, .cancelled:
                finishOnce()
            default:
                break
            }
        }
        connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + .seconds(2), execute: finishOnce)
    }

    private func finish(_ context: NSExtensionContext, _ payload: [String: Any]) {
        let response = NSExtensionItem()
        response.userInfo = [SFExtensionMessageKey: payload]
        context.completeRequest(returningItems: [response], completionHandler: nil)
    }
}

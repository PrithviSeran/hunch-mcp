import Foundation
import Network
import SafariServices
import BridgeShared

final class BridgeServer {
    private struct PendingReceipt {
        let nonce: String
        let completion: (Data) -> Void
    }

    private let queue = DispatchQueue(label: "com.tryhunch.safari.bridge")
    private let listener: NWListener
    private let stateDirectory: URL
    private var pendingReceipts: [String: PendingReceipt] = [:]

    init() throws {
        stateDirectory = try BridgePaths.stateDirectory()
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
        listener = try NWListener(using: parameters)
    }

    func start() throws {
        listener.newConnectionHandler = { [weak self] connection in
            self?.accept(connection)
        }
        listener.stateUpdateHandler = { [weak self] state in
            guard case .ready = state, let port = self?.listener.port else { return }
            do { try self?.writeEndpoint(port: port.rawValue) }
            catch { NSLog("Could not publish Hunch Safari endpoint: %@", error.localizedDescription) }
        }
        listener.start(queue: queue)
    }

    private func writeEndpoint(port: UInt16) throws {
        let data = try JSONSerialization.data(withJSONObject: ["port": Int(port)])
        let url = stateDirectory.appendingPathComponent("endpoint.json")
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    private func accept(_ connection: NWConnection) {
        connection.start(queue: queue)
        receiveLine(from: connection, buffer: Data())
    }

    private func receiveLine(from connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) {
            [weak self] data, _, complete, error in
            guard let self, error == nil else { connection.cancel(); return }
            var collected = buffer
            if let data { collected.append(data) }
            guard collected.count <= 4 * 1024 * 1024 else { connection.cancel(); return }
            if let newline = collected.firstIndex(of: 0x0A) {
                self.route(Data(collected[..<newline])) { response in
                    connection.send(content: response + Data([0x0A]), completion: .contentProcessed { _ in
                        connection.cancel()
                    })
                }
            } else if complete {
                connection.cancel()
            } else {
                self.receiveLine(from: connection, buffer: collected)
            }
        }
    }

    private func route(_ data: Data, completion: @escaping (Data) -> Void) {
        if let receipt = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           receipt["kind"] as? String == "extension_receipt" {
            completeReceipt(receipt)
            completion(response(id: receipt["id"] as? String, status: "verified", reason: "receipt accepted"))
            return
        }
        handle(data, completion: completion)
    }

    private func handle(_ data: Data, completion: @escaping (Data) -> Void) {
        guard let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let identifier = request["id"] as? String,
              UUID(uuidString: identifier) != nil else {
            completion(response(id: nil, status: "refused", reason: "invalid bridge request"))
            return
        }
        guard request["protocol"] as? Int == BridgePaths.protocolVersion else {
            completion(response(id: identifier, status: "refused", reason: "protocol mismatch"))
            return
        }
        guard authenticated(request["token"] as? String) else {
            completion(response(id: identifier, status: "refused", reason: "authentication failed"))
            return
        }
        let allowed = ["status", "open", "tabs", "switch_tab", "snapshot", "act"]
        guard let operation = request["operation"] as? String, allowed.contains(operation) else {
            completion(response(id: identifier, status: "refused", reason: "unsupported operation"))
            return
        }

        SFSafariExtensionManager.getStateOfSafariExtension(
            withIdentifier: BridgePaths.extensionIdentifier
        ) { [weak self] state, error in
            guard let self else { return }
            self.queue.async {
                guard error == nil, state?.isEnabled == true else {
                    if operation != "status" {
                        SFSafariApplication.showPreferencesForExtension(
                            withIdentifier: BridgePaths.extensionIdentifier
                        ) { _ in }
                    }
                    let reason = operation == "status"
                        ? "Hunch is installed but disabled in Safari Settings > Extensions"
                        : "Enable Hunch in Safari Settings > Extensions, then allow website access. Safari Settings was opened for you."
                    completion(self.response(id: identifier, status: "blocked", reason: reason))
                    return
                }
                var forwarded = request
                forwarded.removeValue(forKey: "token")
                self.dispatch(forwarded, id: identifier, completion: completion)
            }
        }
    }

    private func authenticated(_ supplied: String?) -> Bool {
        guard let supplied,
              let expected = try? String(
                contentsOf: stateDirectory.appendingPathComponent("bridge-token"),
                encoding: .utf8
              ).trimmingCharacters(in: .whitespacesAndNewlines) else { return false }
        let left = Array(supplied.utf8), right = Array(expected.utf8)
        guard left.count == right.count else { return false }
        return zip(left, right).reduce(UInt8(0)) { $0 | ($1.0 ^ $1.1) } == 0
    }

    private func dispatch(_ request: [String: Any], id: String,
                          completion: @escaping (Data) -> Void) {
        guard let port = listener.port else {
            completion(response(id: id, status: "failed", reason: "bridge listener is unavailable"))
            return
        }
        let nonce = UUID().uuidString
        pendingReceipts[id] = PendingReceipt(nonce: nonce, completion: completion)
        var forwarded = request
        forwarded["receiptPort"] = Int(port.rawValue)
        forwarded["receiptNonce"] = nonce
        pollReceiptFile(id: id)
        SFSafariApplication.dispatchMessage(
            withName: "hunch-command",
            toExtensionWithIdentifier: BridgePaths.extensionIdentifier,
            userInfo: forwarded
        ) { [weak self] error in
            guard let self else { return }
            if let error {
                self.queue.async {
                    self.pendingReceipts.removeValue(forKey: id)
                    completion(self.response(id: id, status: "failed", reason: error.localizedDescription))
                }
            }
        }
        queue.asyncAfter(deadline: .now() + .seconds(12)) { [weak self] in
            guard let self, let pending = self.pendingReceipts.removeValue(forKey: id) else { return }
            pending.completion(self.response(
                id: id, status: "blocked",
                reason: "Safari extension did not respond; check website access and retry"
            ))
        }
    }

    private func pollReceiptFile(id: String) {
        queue.asyncAfter(deadline: .now() + .milliseconds(50)) { [weak self] in
            guard let self, self.pendingReceipts[id] != nil else { return }
            if let url = try? BridgePaths.receiptURL(id: id),
               let data = try? Data(contentsOf: url),
               let receipt = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                try? FileManager.default.removeItem(at: url)
                self.completeReceipt(receipt)
                return
            }
            self.pollReceiptFile(id: id)
        }
    }

    private func completeReceipt(_ receipt: [String: Any]) {
        guard let id = receipt["id"] as? String,
              let nonce = receipt["receiptNonce"] as? String,
              let pending = pendingReceipts[id],
              constantTimeEqual(nonce, pending.nonce),
              receipt["protocol"] as? Int == BridgePaths.protocolVersion,
              let status = receipt["status"] as? String,
              ["verified", "performed_unverified", "blocked", "refused", "failed"].contains(status),
              JSONSerialization.isValidJSONObject(receipt) else { return }
        var publicReceipt = receipt
        for key in ["kind", "receiptNonce", "receiptPort"] {
            publicReceipt.removeValue(forKey: key)
        }
        guard let data = try? JSONSerialization.data(withJSONObject: publicReceipt) else { return }
        pendingReceipts.removeValue(forKey: id)
        pending.completion(data)
    }

    private func constantTimeEqual(_ left: String, _ right: String) -> Bool {
        let lhs = Array(left.utf8), rhs = Array(right.utf8)
        guard lhs.count == rhs.count else { return false }
        return zip(lhs, rhs).reduce(UInt8(0)) { $0 | ($1.0 ^ $1.1) } == 0
    }

    private func response(id: String?, status: String, reason: String) -> Data {
        var value: [String: Any] = [
            "protocol": BridgePaths.protocolVersion, "status": status, "reason": reason
        ]
        if let id { value["id"] = id }
        return (try? JSONSerialization.data(withJSONObject: value)) ?? Data("{}".utf8)
    }
}

import Darwin
import Foundation

public enum BridgePaths {
    public static let extensionIdentifier = "com.tryhunch.safari.Extension"
    public static let protocolVersion = 1
    public static let companionBundleIdentifier = "com.tryhunch.safari"

    public static func realHomeDirectory() -> URL {
        if let pw = getpwuid(getuid()), let dir = pw.pointee.pw_dir {
            return URL(fileURLWithPath: String(cString: dir), isDirectory: true)
        }
        return URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
    }

    public static func stateDirectory() throws -> URL {
        let directory = realHomeDirectory()
            .appendingPathComponent(
                "Library/Containers/\(companionBundleIdentifier)/Data/Library/Application Support/Hunch",
                isDirectory: true
            )
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        return directory
    }

    public static func receiptURL(id: String) throws -> URL {
        let directory = try stateDirectory().appendingPathComponent("receipts", isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        return directory.appendingPathComponent("\(id).json")
    }
}

public struct BridgeError: LocalizedError {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

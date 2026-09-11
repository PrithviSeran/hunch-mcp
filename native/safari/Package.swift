// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "HunchSafari",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "HunchSafariApp", targets: ["HunchSafariApp"]),
        .executable(name: "HunchSafariExtension", targets: ["HunchSafariExtension"]),
    ],
    targets: [
        .target(name: "BridgeShared", path: "Shared"),
        .executableTarget(
            name: "HunchSafariApp", dependencies: ["BridgeShared"], path: "App",
            exclude: ["Info.plist", "HunchSafari.entitlements", "Resources"]
        ),
        .executableTarget(
            name: "HunchSafariExtension", dependencies: ["BridgeShared"], path: "Extension",
            exclude: ["Info.plist", "HunchSafariExtension.entitlements", "Resources"],
            swiftSettings: [.unsafeFlags(["-application-extension"])]
        ),
    ]
)

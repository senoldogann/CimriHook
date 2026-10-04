// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "CimriHookBar",
    platforms: [.macOS(.v14)],
    targets: [
        .target(name: "CimriHookBarCore"),
        .executableTarget(name: "CimriHookBar", dependencies: ["CimriHookBarCore"]),
        .testTarget(name: "CimriHookBarCoreTests", dependencies: ["CimriHookBarCore"]),
    ]
)

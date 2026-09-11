import Darwin
import Foundation

// Xcode's app-extension linker makes Foundation's NSExtensionMain the executable entry point.
// SwiftPM has no app-extension product type, so this tiny equivalent lets Command Line Tools
// produce the same executable before package.sh assembles the .appex bundle.
@_silgen_name("NSExtensionMain")
private func extensionMain(
    _ argc: Int32,
    _ argv: UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>
) -> Int32

exit(extensionMain(CommandLine.argc, CommandLine.unsafeArgv))

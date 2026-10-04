import Foundation

/// A command that could not run or exited with an error, with what it wrote to stderr.
struct CommandFailure: LocalizedError {
    let command: String
    let status: Int32
    let stderr: String

    var errorDescription: String? {
        let message = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        return message.isEmpty ? "\(command) exited with status \(status)" : message
    }
}

/// Holds stderr while the main thread reads stdout; the dispatch group orders the access.
private final class OutputBox: @unchecked Sendable {
    var data = Data()
}

/// Runs the `cimrihook` CLI the way a terminal would.
///
/// Apps started from Finder or at login do not inherit the terminal's PATH, so the PATH of the
/// user's login shell is read once and passed on; `cimrihook quota` needs it to find `claude`
/// and `codex` as well.
enum CommandRunner {
    static func cimrihook(_ arguments: [String], _ path: String) async throws -> Data {
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = path
        let env = URL(fileURLWithPath: "/usr/bin/env")
        return try await run(env, ["cimrihook"] + arguments, environment)
    }

    /// The PATH a login shell of the current user sets up.
    static func loginPath() async throws -> String {
        guard let entry = getpwuid(getuid()), let shell = entry.pointee.pw_shell else {
            throw CommandFailure(command: "getpwuid", status: errno, stderr: "no login shell")
        }
        let data = try await run(
            URL(fileURLWithPath: String(cString: shell)),
            ["-l", "-c", "printf %s \"$PATH\""],
            ProcessInfo.processInfo.environment
        )
        return String(decoding: data, as: UTF8.self)
    }

    static func run(_ executable: URL, _ arguments: [String], _ environment: [String: String])
        async throws -> Data
    {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                continuation.resume(with: Result { try blocking(executable, arguments, environment) })
            }
        }
    }

    private static func blocking(
        _ executable: URL, _ arguments: [String], _ environment: [String: String]
    ) throws -> Data {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        let command = ([executable.lastPathComponent] + arguments).joined(separator: " ")
        do {
            try process.run()
        } catch {
            throw CommandFailure(command: command, status: -1, stderr: "\(error)")
        }
        // stderr is drained on another thread so that neither pipe can fill and stall the child.
        let errorText = OutputBox()
        let readers = DispatchGroup()
        let errorHandle = errors.fileHandleForReading
        DispatchQueue.global(qos: .utility).async(group: readers) {
            errorText.data = errorHandle.readDataToEndOfFile()
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        readers.wait()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw CommandFailure(
                command: command,
                status: process.terminationStatus,
                stderr: String(decoding: errorText.data, as: UTF8.self)
            )
        }
        return data
    }
}

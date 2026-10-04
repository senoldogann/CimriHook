import CimriHookBarCore
import Foundation
import Observation

/// A value the panel is waiting for, has, or could not get.
enum Loaded<Value: Sendable>: Sendable {
    case loading
    case loaded(Value)
    case failed(String)

    var value: Value? {
        if case .loaded(let value) = self { return value }
        return nil
    }
}

/// The panel's state: both providers' windows, what a Codex point costs and the gain since the
/// last `cimrihook init`.
///
/// The quota probes open a control connection to each CLI and send no model request, so
/// reading them does not use the windows they report.
@MainActor
@Observable
final class PanelStore {
    static let refreshInterval: Duration = .seconds(300)

    var claude: Loaded<QuotaSnapshot> = .loading
    var codex: Loaded<QuotaSnapshot> = .loading
    var gain: Loaded<Gain> = .loading
    var codexCost: Loaded<[PointCost]> = .loading
    var refreshedAt: Date?
    var refreshing = false

    private var path: String?

    init() {
        Task { await self.refreshForever() }
    }

    var barTitle: String { CimriHookBarCore.barTitle(claude.value, codex.value) }

    func refreshForever() async {
        while !Task.isCancelled {
            await refresh()
            try? await Task.sleep(for: Self.refreshInterval)
        }
    }

    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        let path: String
        do {
            path = try await resolvedPath()
        } catch {
            let message = "Cannot read the login shell's PATH: \(error.localizedDescription)"
            (claude, codex, gain) = (.failed(message), .failed(message), .failed(message))
            codexCost = .failed(message)
            return
        }
        async let claudeReading = load(["quota", "--agent", "claude"], path, decodeQuota)
        async let codexReading = load(["quota", "--agent", "codex"], path, decodeQuota)
        async let gainReading = load(["gain", "--json"], path, decodeGain)
        async let costReading = load(
            ["limits", "--agent", "codex", "--days", "30", "--json"], path, decodePointCosts)
        (claude, codex, gain) = await (claudeReading, codexReading, gainReading)
        codexCost = await costReading
        refreshedAt = Date()
    }

    private func resolvedPath() async throws -> String {
        if let path { return path }
        let found = try await CommandRunner.loginPath()
        path = found
        return found
    }

    private nonisolated func load<Value: Sendable>(
        _ arguments: [String], _ path: String, _ decode: (Data) throws -> Value
    ) async -> Loaded<Value> {
        do {
            return .loaded(try decode(try await CommandRunner.cimrihook(arguments, path)))
        } catch {
            return .failed(error.localizedDescription)
        }
    }
}

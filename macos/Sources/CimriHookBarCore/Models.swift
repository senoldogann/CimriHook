import Foundation

/// One subscription window as `cimrihook quota` reports it: the provider's own percentage.
public struct QuotaWindow: Decodable, Sendable, Equatable {
    public let id: String
    public let usedPercent: Double
    public let resetsAt: String?
    public let durationMinutes: Int?
}

/// An account-wide reading of one provider's windows; not the usage of a single task.
public struct QuotaSnapshot: Decodable, Sendable, Equatable {
    public let provider: String
    public let observedAt: String
    public let available: Bool
    public let windows: [QuotaWindow]
}

/// The requests and list-price spend of one period of `cimrihook gain`.
public struct GainPeriod: Decodable, Sendable, Equatable {
    public let start: Double
    public let end: Double
    public let requests: Int
    public let usd: Double
    public let meanContext: Double
    public let largeContextUsd: Double
    public let compactions: Int
    public let idleRewriteUsd: Double
    public let guardStops: Int
}

/// The matched replay of the sessions that compacted automatically since the install.
public struct GainReceipt: Decodable, Sendable, Equatable {
    public let compactions: Int
    public let sessions: Int
    public let requestsUsd: Double
    public let compactionCallsUsd: Double
    public let withoutCompactionsUsd: Double
}

/// `cimrihook gain --json`: the time since the last init against the same time before it.
public struct Gain: Decodable, Sendable, Equatable {
    public let installedAt: Double
    public let before: GainPeriod
    public let after: GainPeriod
    public let receipt: GainReceipt
}

/// What a point of one Codex window costs, from `cimrihook limits --agent codex --json`.
///
/// Token counts are nil when the fit cannot tell them apart from zero.
public struct PointCost: Decodable, Sendable, Equatable {
    public let kind: String
    public let spans: Int
    public let points: Double
    public let inputTokens: Double?
    public let outputTokens: Double?
    public let outputShare: Double?
    public let baseUnits: Double?
}

/// A payload that `cimrihook` printed but this panel cannot read.
public struct PayloadError: LocalizedError, Equatable {
    public let what: String
    public let detail: String

    public var errorDescription: String? { "\(what): \(detail)" }
}

/// Reads the output of `cimrihook quota --agent <provider>`.
public func decodeQuota(_ data: Data) throws -> QuotaSnapshot {
    try decode(QuotaSnapshot.self, data, "cimrihook quota")
}

/// Reads the output of `cimrihook gain --json`.
public func decodeGain(_ data: Data) throws -> Gain {
    try decode(Gain.self, data, "cimrihook gain")
}

/// Reads the output of `cimrihook limits --agent codex --json`.
public func decodePointCosts(_ data: Data) throws -> [PointCost] {
    try decode([PointCost].self, data, "cimrihook limits")
}

func decode<T: Decodable>(_ type: T.Type, _ data: Data, _ what: String) throws -> T {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    do {
        return try decoder.decode(type, from: data)
    } catch let error as DecodingError {
        let body = String(decoding: data.prefix(300), as: UTF8.self)
        throw PayloadError(what: what, detail: "\(error) in \(body)")
    }
}

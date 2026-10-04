import Foundation

/// How full a window is, for its colour.
public enum Level: Sendable, Equatable {
    case comfortable  // below 70%
    case tight  // 70% to 90%
    case critical  // 90% and above
}

/// The level of a used percentage.
public func level(_ usedPercent: Double) -> Level {
    if usedPercent >= 90 { return .critical }
    if usedPercent >= 70 { return .tight }
    return .comfortable
}

private let modelScopedPrefix = "seven_day_model:"

/// A window's name from its length, with the model for Claude's model-scoped weekly windows.
public func windowLabel(_ window: QuotaWindow) -> String {
    let length: String
    switch window.durationMinutes {
    case 300: length = "5-hour"
    case 10_080: length = "Weekly"
    case let minutes?: length = "\(minutes / 60)-hour"
    case nil: length = window.id
    }
    guard window.id.hasPrefix(modelScopedPrefix) else { return length }
    return "\(length) · \(window.id.dropFirst(modelScopedPrefix.count))"
}

/// The reset moment of a window; the quota command writes ISO 8601 with or without fractions.
public func resetDate(_ text: String) throws -> Date {
    let fractional = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
    let whole = Date.ISO8601FormatStyle(includingFractionalSeconds: false)
    if let date = try? fractional.parse(text) { return date }
    if let date = try? whole.parse(text) { return date }
    throw PayloadError(what: "reset time", detail: "not ISO 8601: \(text)")
}

/// Time left until a reset: `2 h 05 min`, `4 d 23 h`, `now` once it has passed.
public func countdown(_ now: Date, _ reset: Date) -> String {
    let minutes = Int((reset.timeIntervalSince(now) / 60).rounded(.up))
    if minutes <= 0 { return "now" }
    if minutes < 60 { return "\(minutes) min" }
    if minutes < 24 * 60 { return "\(minutes / 60) h \(String(format: "%02d", minutes % 60)) min" }
    return "\(minutes / (24 * 60)) d \(minutes % (24 * 60) / 60) h"
}

/// The text in the menu bar: each provider's fullest window, `C 38%  X 100%`.
public func barTitle(_ claude: QuotaSnapshot?, _ codex: QuotaSnapshot?) -> String {
    let parts = [("C", claude), ("X", codex)].compactMap { letter, snapshot -> String? in
        guard let fullest = snapshot?.windows.map(\.usedPercent).max() else { return nil }
        return "\(letter) \(Int(fullest.rounded()))%"
    }
    return parts.isEmpty ? "CimriHook" : parts.joined(separator: "  ")
}

/// Relative change from one value to another; nil when there is nothing to compare against.
public func relativeChange(_ before: Double, _ after: Double) -> Double? {
    before > 0 ? (after - before) / before : nil
}

/// The ratios `cimrihook gain` prints that depend least on how much was worked.
public struct GainSummary: Sendable, Equatable {
    public let days: Double
    public let spendPerRequestChange: Double?
    public let meanContextChange: Double?
    public let largeContextShareBefore: Double?
    public let largeContextShareAfter: Double?
    public let receiptSavedUsd: Double?  // nil until a compaction has happened
    public let receiptChange: Double?
}

/// The summary of a gain measurement, computed as `cimrihook gain` does.
public func summarize(_ gain: Gain) -> GainSummary {
    let actual = gain.receipt.requestsUsd + gain.receipt.compactionCallsUsd
    let compacted = gain.receipt.compactions > 0
    return GainSummary(
        days: (gain.after.end - gain.after.start) / 86_400,
        spendPerRequestChange: relativeChange(perRequest(gain.before), perRequest(gain.after)),
        meanContextChange: relativeChange(gain.before.meanContext, gain.after.meanContext),
        largeContextShareBefore: share(gain.before.largeContextUsd, gain.before.usd),
        largeContextShareAfter: share(gain.after.largeContextUsd, gain.after.usd),
        receiptSavedUsd: compacted ? gain.receipt.withoutCompactionsUsd - actual : nil,
        receiptChange: compacted ? relativeChange(gain.receipt.withoutCompactionsUsd, actual) : nil
    )
}

func perRequest(_ period: GainPeriod) -> Double {
    period.requests > 0 ? period.usd / Double(period.requests) : 0
}

func share(_ part: Double, _ whole: Double) -> Double? {
    whole > 0 ? part / whole : nil
}

/// A signed whole percentage, `-48%`, or a dash when there is no change to show.
public func percentText(_ ratio: Double?) -> String {
    guard let ratio else { return "–" }
    return String(format: "%+.0f%%", ratio * 100)
}

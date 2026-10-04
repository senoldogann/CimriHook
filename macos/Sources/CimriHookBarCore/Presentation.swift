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

/// The use of a provider's fullest window, which the menu bar shows; nil without a reading.
public func fullestPercent(_ snapshot: QuotaSnapshot?) -> Double? {
    snapshot?.windows.map(\.usedPercent).max()
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

/// The spend per request of a period, 0 without requests.
public func perRequest(_ period: GainPeriod) -> Double {
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

/// A token count as `99K` or `5.0K`, `1.2M`.
public func tokenText(_ tokens: Double) -> String {
    if tokens >= 999_500 { return String(format: "%.1fM", tokens / 1_000_000) }
    if tokens >= 9_950 { return String(format: "%.0fK", tokens / 1_000) }
    if tokens >= 1_000 { return String(format: "%.1fK", tokens / 1_000) }
    return String(format: "%.0f", tokens)
}

/// The window a point cost belongs to: `5-hour`, `Weekly`.
public func pointCostLabel(_ cost: PointCost) -> String {
    switch cost.kind {
    case "five_hour": "5-hour"
    case "seven_day": "Weekly"
    default: cost.kind
    }
}

/// `99K in or 5.0K out · output 38%`, the pooled base units when input and output cannot be told
/// apart, nil when nothing is measured yet.
public func pointCostText(_ cost: PointCost) -> String? {
    if let input = cost.inputTokens, let output = cost.outputTokens {
        let share = cost.outputShare.map { " · output \(Int(($0 * 100).rounded()))%" } ?? ""
        return "\(tokenText(input)) in or \(tokenText(output)) out\(share)"
    }
    return cost.baseUnits.map { "\(tokenText($0)) base units" }
}


private let minimumPaceSeconds: TimeInterval = 15 * 60

/// Where a window is heading at the rate it filled since it opened.
public enum Pace: Sendable, Equatable {
    case full  // already at 100%
    case fillsAt(Date)  // reaches 100% before it resets
    case lastsUntilReset
}

/// The pace of a window from its use since it opened (reset time minus its length).
///
/// Nil without a length or a reset time, before any use, or in its first quarter hour, when a
/// few requests would make the rate say more than it knows.
public func pace(_ window: QuotaWindow, _ now: Date) throws -> Pace? {
    if window.usedPercent >= 100 { return .full }
    guard let minutes = window.durationMinutes, let text = window.resetsAt,
        window.usedPercent > 0
    else { return nil }
    let reset = try resetDate(text)
    let elapsed = now.timeIntervalSince(reset.addingTimeInterval(-Double(minutes) * 60))
    guard elapsed >= minimumPaceSeconds else { return nil }
    let fills = now.addingTimeInterval((100 - window.usedPercent) / (window.usedPercent / elapsed))
    return fills < reset ? .fillsAt(fills) : .lastsUntilReset
}

/// `full until it resets`, `full in 47 min at this pace`, `lasts until it resets at this pace`.
public func paceText(_ pace: Pace, _ now: Date) -> String {
    switch pace {
    case .full: "full until it resets"
    case .fillsAt(let date): "full in \(countdown(now, date)) at this pace"
    case .lastsUntilReset: "lasts until it resets at this pace"
    }
}

/// The account-wide 5-hour and weekly windows of a reading; model-scoped windows are left out.
public struct MainWindows: Sendable, Equatable {
    public let fiveHour: QuotaWindow?
    public let weekly: QuotaWindow?
}

/// The 5-hour and weekly windows that the ring gauges show.
public func mainWindows(_ snapshot: QuotaSnapshot) -> MainWindows {
    let shared = snapshot.windows.filter { !$0.id.hasPrefix(modelScopedPrefix) }
    return MainWindows(
        fiveHour: shared.first { $0.durationMinutes == 300 },
        weekly: shared.first { $0.durationMinutes == 10_080 }
    )
}

/// The windows the rings do not show, such as Claude's model-scoped weekly windows.
public func otherWindows(_ snapshot: QuotaSnapshot) -> [QuotaWindow] {
    let main = mainWindows(snapshot)
    return snapshot.windows.filter { $0 != main.fiveHour && $0 != main.weekly }
}

import CimriHookBarCore
import Foundation
import Testing

// Payloads in the shape `cimrihook quota` and `cimrihook gain --json` print.
let claudeQuota = Data(
    """
    {"provider": "claude", "observed_at": "2026-10-04T21:48:59.085649+00:00", "available": true,
     "account_fingerprint": null, "windows": [
      {"id": "five_hour", "used_percent": 38.0,
       "resets_at": "2026-10-04T23:10:00.387986+00:00", "duration_minutes": 300},
      {"id": "seven_day_model:Fable", "used_percent": 5.0,
       "resets_at": "2026-10-09T21:00:00+00:00", "duration_minutes": 10080}]}
    """.utf8)

let gainOutput = Data(
    """
    {"installed_at": 1000.0,
     "before": {"start": 0, "end": 1000.0, "requests": 4, "usd": 2.0, "mean_context": 300000,
       "large_context_usd": 1.5, "compactions": 0, "idle_rewrite_usd": 0, "guard_stops": 0},
     "after": {"start": 1000.0, "end": 87400.0, "requests": 4, "usd": 1.0, "mean_context": 100000,
       "large_context_usd": 0, "compactions": 2, "idle_rewrite_usd": 0, "guard_stops": 0},
     "receipt": {"compactions": 2, "sessions": 1, "requests_usd": 0.9,
       "compaction_calls_usd": 0.1, "without_compactions_usd": 1.25}}
    """.utf8)

@Test func quotaWindowsReadWithTheirLabelsAndResets() throws {
    let snapshot = try decodeQuota(claudeQuota)
    #expect(snapshot.windows.map(windowLabel) == ["5-hour", "Weekly · Fable"])
    let reset = try resetDate(try #require(snapshot.windows[0].resetsAt))
    #expect(countdown(reset.addingTimeInterval(-81 * 60), reset) == "1 h 21 min")
    #expect(countdown(reset.addingTimeInterval(-(4 * 86_400 + 23 * 3_600)), reset) == "4 d 23 h")
    #expect(countdown(reset.addingTimeInterval(60), reset) == "now")
    #expect(barTitle(snapshot, nil) == "C 38%")
}

@Test func gainSummaryMatchesTheCliRatios() throws {
    let summary = summarize(try decodeGain(gainOutput))
    #expect(summary.days == 1)
    #expect(percentText(summary.spendPerRequestChange) == "-50%")
    #expect(percentText(summary.meanContextChange) == "-67%")
    #expect(summary.receiptSavedUsd == 0.25)
    #expect(percentText(summary.receiptChange) == "-20%")
}

@Test func aMalformedPayloadNamesTheCommand() {
    #expect(throws: PayloadError.self) { try decodeQuota(Data("{}".utf8)) }
}

@Test func codexPointCostReadsBothFits() throws {
    let costs = try decodePointCosts(Data(
        """
        [{"kind": "five_hour", "spans": 1629, "points": 2194.0, "input_tokens": 99329.0,
          "output_tokens": 5024.6, "output_share": 0.38, "base_units": 75951.3},
         {"kind": "seven_day", "spans": 350, "points": 478.0, "input_tokens": null,
          "output_tokens": null, "output_share": null, "base_units": 442423.4}]
        """.utf8))
    #expect(costs.map(pointCostLabel) == ["5-hour", "Weekly"])
    #expect(pointCostText(costs[0]) == "99K in or 5.0K out · output 38%")
    #expect(pointCostText(costs[1]) == "442K base units")
}


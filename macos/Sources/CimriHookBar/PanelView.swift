import AppKit
import CimriHookBarCore
import SwiftUI

/// The panel that opens from the menu bar: one card per provider, then the gain, scrolling
/// inside a fixed height between a fixed header and footer.
struct PanelView: View {
    let store: PanelStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ProviderCard(logo: .claude, title: "Claude Code", reading: store.claude) {
                        EmptyView()
                    }
                    ProviderCard(logo: .openAI, title: "Codex", reading: store.codex) {
                        PointCostLines(reading: store.codexCost)
                    }
                    Card { GainSection(reading: store.gain) }
                }
            }
            .scrollIndicators(.automatic)
            .frame(height: 520)
            footer
        }
        .padding(12)
        .frame(width: 320)
    }

    private var header: some View {
        HStack {
            Text("CimriHook").font(.headline)
            Spacer()
            if store.refreshing {
                ProgressView().controlSize(.small)
            } else {
                Button {
                    Task { await store.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise").font(.callout.weight(.medium))
                }
                .buttonStyle(.borderless)
                .keyboardShortcut("r")
                .help("Read the windows again (⌘R)")
            }
        }
        .padding(.horizontal, 4)
    }

    private var footer: some View {
        HStack {
            if let refreshedAt = store.refreshedAt {
                Text("Updated \(refreshedAt.formatted(date: .omitted, time: .shortened))")
            }
            Spacer()
            Button("Quit") { NSApplication.shared.terminate(nil) }
                .buttonStyle(.borderless)
                .keyboardShortcut("q")
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 4)
    }
}

/// A rounded, filled card like the modules of Control Center.
struct Card<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        content
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(.fill.quaternary, in: .rect(cornerRadius: 12, style: .continuous))
    }
}

/// One provider: its mark and name, then one row per window.
struct ProviderCard<Extra: View>: View {
    let logo: ProviderLogo
    let title: String
    let reading: Loaded<QuotaSnapshot>
    @ViewBuilder let extra: Extra

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 7) {
                    Image(nsImage: logo.image).renderingMode(.template)
                        .resizable()
                        .frame(width: 16, height: 16)
                        .foregroundStyle(logo.tint)
                    Text(title).font(.subheadline.weight(.semibold))
                    Spacer()
                    if case .loaded(let snapshot) = reading, let fullest = fullestPercent(snapshot) {
                        Circle()
                            .fill(levelColor(level(fullest)))
                            .frame(width: 7, height: 7)
                            .help("Fullest window \(Int(fullest.rounded()))%")
                    }
                }
                windows
                extra
            }
        }
    }

    @ViewBuilder
    private var windows: some View {
        switch reading {
        case .loading:
            Text("Reading…").font(.caption).foregroundStyle(.secondary)
        case .failed(let message):
            FailureText(message: message)
        case .loaded(let snapshot) where !snapshot.available || snapshot.windows.isEmpty:
            Text("This account reports no subscription windows.")
                .font(.caption)
                .foregroundStyle(.secondary)
        case .loaded(let snapshot):
            ForEach(snapshot.windows, id: \.id) { window in
                WindowRow(window: window)
            }
        }
    }
}

/// A window: its name and use, a bar, when it resets and where it is heading.
struct WindowRow: View {
    let window: QuotaWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(windowLabel(window)).font(.callout)
                Spacer()
                Text("\(Int(window.usedPercent.rounded()))%")
                    .font(.system(.title3, design: .rounded).weight(.semibold).monospacedDigit())
                    .contentTransition(.numericText())
                    .animation(.smooth, value: window.usedPercent)
            }
            UsageBar(percent: window.usedPercent)
            TimelineView(.periodic(from: .now, by: 60)) { context in
                HStack {
                    if let reset = try? window.resetsAt.map(resetDate) {
                        Text("Resets in \(countdown(context.date, reset)) · \(resetMoment(reset, context.date))")
                            .help(reset.formatted(date: .complete, time: .shortened))
                    }
                    Spacer()
                    if let heading = try? pace(window, context.date) {
                        Text(paceText(heading, context.date))
                            .foregroundStyle(paceColor(heading))
                            .help("At the rate it has filled since it opened")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
    }

    /// The reset's clock time, with the weekday when it is not today: `02:09`, `Sat 23:59`.
    private func resetMoment(_ reset: Date, _ now: Date) -> String {
        Calendar.current.isDate(reset, inSameDayAs: now)
            ? reset.formatted(date: .omitted, time: .shortened)
            : reset.formatted(.dateTime.weekday(.abbreviated).hour().minute())
    }

    private func paceColor(_ heading: Pace) -> Color {
        switch heading {
        case .full: .red
        case .fillsAt: .orange
        case .lastsUntilReset: .secondary
        }
    }
}

/// A thin capsule filled to the used share, coloured by how full the window is.
struct UsageBar: View {
    let percent: Double

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(.fill.secondary)
                Capsule()
                    .fill(levelColor(level(percent)).gradient)
                    .frame(width: geometry.size.width * min(max(percent, 0), 100) / 100)
            }
        }
        .frame(height: 6)
        .animation(.smooth(duration: 0.5), value: percent)
    }
}

/// What a point of each Codex window cost over the last 30 days of rollouts.
struct PointCostLines: View {
    let reading: Loaded<[PointCost]>

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("1 point · last 30 days").font(.caption.weight(.medium))
            switch reading {
            case .loading:
                Text("Reading…").foregroundStyle(.secondary)
            case .failed(let message):
                FailureText(message: message)
            case .loaded(let costs) where costs.isEmpty:
                Text("No rollouts yet").foregroundStyle(.secondary)
            case .loaded(let costs):
                ForEach(costs, id: \.kind) { cost in
                    VStack(alignment: .leading, spacing: 1) {
                        Text("\(pointCostLabel(cost)) ≈ \(pointCostText(cost) ?? "too few readings")")
                        Text(verbatim: "\(cost.spans) spans · \(Int(cost.points.rounded())) pts\(cost.inputTokens != nil && cost.outputTokens != nil ? "" : " · pooled")")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    .help("Fitted over the last 30 days of Codex rollouts, between whole-percent crossings")
                }
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }
}

/// What the governor changed since the last `cimrihook init`.
struct GainSection: View {
    let reading: Loaded<Gain>

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Since cimrihook init").font(.subheadline.weight(.semibold))
            switch reading {
            case .loading:
                Text("Reading…").font(.caption).foregroundStyle(.secondary)
            case .failed(let message):
                FailureText(message: message)
            case .loaded(let gain):
                content(gain)
            }
        }
    }

    @ViewBuilder
    private func content(_ gain: Gain) -> some View {
        let summary = summarize(gain)
        Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 3) {
            GridRow {
                Text("")
                Text("Before").gridColumnAlignment(.trailing)
                Text("After").gridColumnAlignment(.trailing)
                Text("Change").gridColumnAlignment(.trailing)
            }
            .foregroundStyle(.tertiary)
            CompareRow(
                label: "Per request",
                before: String(format: "$%.3f", perRequest(gain.before)),
                after: String(format: "$%.3f", perRequest(gain.after)),
                change: percentText(summary.spendPerRequestChange))
            CompareRow(
                label: "Mean context",
                before: tokenText(gain.before.meanContext),
                after: tokenText(gain.after.meanContext),
                change: percentText(summary.meanContextChange))
            CompareRow(
                label: "Over 200k",
                before: shareText(summary.largeContextShareBefore),
                after: shareText(summary.largeContextShareAfter),
                change: "")
            CompareRow(
                label: "Requests", before: "\(gain.before.requests)", after: "\(gain.after.requests)", change: "")
            CompareRow(label: "Spend", before: usd(gain.before.usd), after: usd(gain.after.usd), change: "")
            CompareRow(
                label: "Compactions", before: "\(gain.before.compactions)", after: "\(gain.after.compactions)",
                change: "")
            CompareRow(
                label: "Idle re-cache", before: usd(gain.before.idleRewriteUsd),
                after: usd(gain.after.idleRewriteUsd), change: "")
            CompareRow(
                label: "Guard stops", before: "\(gain.before.guardStops)", after: "\(gain.after.guardStops)",
                change: "")
        }
        .font(.caption.monospacedDigit())
        .foregroundStyle(.secondary)
        receipt(gain.receipt, summary)
        Text(String(format: "%.1f days each, since %@ · list prices, not a bill · before/after, not A/B",
            summary.days, Date(timeIntervalSince1970: gain.installedAt).formatted(date: .numeric, time: .shortened)))
            .font(.caption2)
            .foregroundStyle(.tertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder
    private func receipt(_ receipt: GainReceipt, _ summary: GainSummary) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            if let saved = summary.receiptSavedUsd {
                Text("Compactions saved \(usd(saved)) (\(percentText(summary.receiptChange)))")
                    .foregroundStyle(.primary)
                Text("\(receipt.compactions) in \(receipt.sessions) sessions · \(usd(receipt.requestsUsd)) + \(usd(receipt.compactionCallsUsd)) calls vs \(usd(receipt.withoutCompactionsUsd)) without")
            } else {
                Text("No compaction since init yet")
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func usd(_ value: Double) -> String { String(format: "$%.2f", value) }

    private func shareText(_ ratio: Double?) -> String {
        ratio.map { String(format: "%.0f%%", $0 * 100) } ?? "–"
    }
}

/// One measure before and after init, with its relative change where it means something.
struct CompareRow: View {
    let label: String
    let before: String
    let after: String
    let change: String

    var body: some View {
        GridRow {
            Text(label)
            Text(before).gridColumnAlignment(.trailing)
            Text(after).gridColumnAlignment(.trailing).foregroundStyle(.primary)
            Text(change).gridColumnAlignment(.trailing).foregroundStyle(.primary)
        }
    }
}

struct FailureText: View {
    let message: String

    var body: some View {
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .font(.caption)
            .foregroundStyle(.orange)
            .fixedSize(horizontal: false, vertical: true)
            .lineLimit(4)
    }
}

/// The colour of a level, shared by the bars.
func levelColor(_ level: Level) -> Color {
    switch level {
    case .comfortable: .green
    case .tight: .orange
    case .critical: .red
    }
}

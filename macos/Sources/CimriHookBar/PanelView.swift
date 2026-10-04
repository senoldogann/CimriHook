import AppKit
import CimriHookBarCore
import SwiftUI

/// A page of the panel, chosen in the rail on its left.
enum PanelTab: Hashable {
    case claude, codex, gain
}

/// The panel that opens from the menu bar: a rail of tabs on the left, the chosen page on the right.
struct PanelView: View {
    let store: PanelStore
    @State private var tab: PanelTab = .claude

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            HStack(alignment: .top, spacing: 10) {
                VStack(spacing: 6) {
                    RailButton(selection: $tab, tab: .claude, shortcut: "1", caption: railPercent(store.claude)) {
                        logoMark(.claude)
                    }
                    RailButton(selection: $tab, tab: .codex, shortcut: "2", caption: railPercent(store.codex)) {
                        logoMark(.openAI)
                    }
                    RailButton(selection: $tab, tab: .gain, shortcut: "3", caption: "Gain") {
                        Image(systemName: "chart.line.downtrend.xyaxis").font(.system(size: 14, weight: .medium))
                    }
                }
                page
            }
            footer
        }
        .padding(12)
        .frame(width: 340)
    }

    @ViewBuilder
    private var page: some View {
        switch tab {
        case .claude:
            ProviderCard(logo: .claude, title: "Claude Code", reading: store.claude) {
                EmptyView()
            }
        case .codex:
            ProviderCard(logo: .openAI, title: "Codex", reading: store.codex) {
                PointCostLines(reading: store.codexCost)
            }
        case .gain:
            Card { GainSection(reading: store.gain) }
        }
    }

    private func logoMark(_ logo: ProviderLogo) -> some View {
        Image(nsImage: logo.image).renderingMode(.template)
            .resizable()
            .frame(width: 16, height: 16)
            .foregroundStyle(logo.tint)
    }

    private func railPercent(_ reading: Loaded<QuotaSnapshot>) -> String {
        guard case .loaded(let snapshot) = reading, let fullest = fullestPercent(snapshot) else { return "–" }
        return "\(Int(fullest.rounded()))%"
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

/// A tab in the rail: its mark over a short caption, filled while it is chosen.
struct RailButton<Mark: View>: View {
    @Binding var selection: PanelTab
    let tab: PanelTab
    let shortcut: KeyEquivalent
    let caption: String
    @ViewBuilder let mark: Mark

    var body: some View {
        Button {
            selection = tab
        } label: {
            VStack(spacing: 3) {
                mark.frame(height: 18)
                Text(caption)
                    .font(.system(size: 10, weight: .medium).monospacedDigit())
                    .foregroundStyle(selection == tab ? .primary : .secondary)
            }
            .frame(width: 44, height: 46)
            .background(
                selection == tab ? AnyShapeStyle(.fill.secondary) : AnyShapeStyle(.clear),
                in: .rect(cornerRadius: 9, style: .continuous)
            )
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .keyboardShortcut(shortcut)
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
                VStack(alignment: .leading, spacing: 2) {
                    if let reset = try? window.resetsAt.map(resetDate) {
                        Text("Resets in \(countdown(context.date, reset)) · \(resetMoment(reset, context.date))")
                            .help(reset.formatted(date: .complete, time: .shortened))
                    }
                    if let heading = try? pace(window, context.date) {
                        let sentence = paceText(heading, context.date)
                        Text(sentence.prefix(1).uppercased() + sentence.dropFirst())
                            .foregroundStyle(paceColor(heading))
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
            Text("1 point over the last 30 days").font(.caption.weight(.medium))
            switch reading {
            case .loading:
                Text("Reading rollouts…").foregroundStyle(.secondary)
            case .failed(let message):
                FailureText(message: message)
            case .loaded(let costs) where costs.isEmpty:
                Text("No Codex rollouts with window readings yet.").foregroundStyle(.secondary)
            case .loaded(let costs):
                ForEach(costs, id: \.kind) { cost in
                    VStack(alignment: .leading, spacing: 1) {
                        Text("\(pointCostLabel(cost)) ≈ \(pointCostText(cost) ?? "not enough readings yet")")
                        Text(verbatim: "\(cost.spans) spans · \(Int(cost.points.rounded())) points\(cost.inputTokens != nil && cost.outputTokens != nil ? "" : " · in and out pooled")")
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
        HStack(alignment: .top, spacing: 12) {
            Figure(value: percentText(summary.spendPerRequestChange), label: "Per request")
            Figure(value: percentText(summary.meanContextChange), label: "Context")
            if let saved = summary.receiptSavedUsd {
                Figure(value: String(format: "$%.0f", saved), label: "Saved")
            }
        }
        Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 3) {
            GridRow {
                Text("")
                Text("Before").gridColumnAlignment(.trailing)
                Text("After").gridColumnAlignment(.trailing)
            }
            .foregroundStyle(.tertiary)
            CompareRow(label: "Requests", before: "\(gain.before.requests)", after: "\(gain.after.requests)")
            CompareRow(label: "Spend", before: usd(gain.before.usd), after: usd(gain.after.usd))
            CompareRow(
                label: "Per request",
                before: String(format: "$%.3f", perRequest(gain.before)),
                after: String(format: "$%.3f", perRequest(gain.after)))
            CompareRow(
                label: "Mean context",
                before: tokenText(gain.before.meanContext),
                after: tokenText(gain.after.meanContext))
            CompareRow(
                label: "Spend over 200k",
                before: shareText(summary.largeContextShareBefore),
                after: shareText(summary.largeContextShareAfter))
            CompareRow(
                label: "Compactions", before: "\(gain.before.compactions)", after: "\(gain.after.compactions)")
            CompareRow(
                label: "Idle re-cache", before: usd(gain.before.idleRewriteUsd), after: usd(gain.after.idleRewriteUsd))
            CompareRow(label: "Guard stops", before: "\(gain.before.guardStops)", after: "\(gain.after.guardStops)")
        }
        .font(.caption.monospacedDigit())
        .foregroundStyle(.secondary)
        receipt(gain.receipt, summary)
        VStack(alignment: .leading, spacing: 2) {
            Text(String(format: "Periods compared: %.1f days each, since %@", summary.days,
                Date(timeIntervalSince1970: gain.installedAt).formatted(date: .abbreviated, time: .shortened)))
            Text("API list prices, not a subscription bill. Before/after view, not an A/B test.")
        }
        .font(.caption2)
        .foregroundStyle(.tertiary)
        .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder
    private func receipt(_ receipt: GainReceipt, _ summary: GainSummary) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Compaction receipt").font(.caption.weight(.medium))
            if let saved = summary.receiptSavedUsd {
                Text("\(usd(saved)) saved (\(percentText(summary.receiptChange))) over \(receipt.compactions) compactions in \(receipt.sessions) sessions")
                Text("\(usd(receipt.requestsUsd)) requests + \(usd(receipt.compactionCallsUsd)) compaction calls vs \(usd(receipt.withoutCompactionsUsd)) without")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
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

/// One measure before and after init.
struct CompareRow: View {
    let label: String
    let before: String
    let after: String

    var body: some View {
        GridRow {
            Text(label)
            Text(before).gridColumnAlignment(.trailing)
            Text(after).gridColumnAlignment(.trailing).foregroundStyle(.primary)
        }
    }
}

/// A large number over its caption.
struct Figure: View {
    let value: String
    let label: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.system(.title2, design: .rounded).weight(.semibold).monospacedDigit())
                .contentTransition(.numericText())
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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

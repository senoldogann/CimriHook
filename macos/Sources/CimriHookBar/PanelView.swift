import AppKit
import CimriHookBarCore
import SwiftUI

/// The panel that opens from the menu bar.
struct PanelView: View {
    let store: PanelStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            ProviderSection(title: "Claude Code", reading: store.claude)
            Divider()
            ProviderSection(title: "Codex", reading: store.codex)
            PointCostSection(reading: store.codexCost)
            Divider()
            GainSection(reading: store.gain)
            Divider()
            footer
        }
        .padding(14)
        .frame(width: 330)
    }

    private var header: some View {
        HStack {
            Text("CimriHook").font(.headline)
            Spacer()
            if store.refreshing {
                ProgressView().controlSize(.small)
            }
            Button {
                Task { await store.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .disabled(store.refreshing)
            .help("Read the windows again")
        }
    }

    private var footer: some View {
        HStack {
            if let refreshedAt = store.refreshedAt {
                Text("Updated \(refreshedAt.formatted(date: .omitted, time: .shortened))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Quit") { NSApplication.shared.terminate(nil) }
                .buttonStyle(.borderless)
                .font(.caption)
        }
    }
}

/// One provider's subscription windows.
struct ProviderSection: View {
    let title: String
    let reading: Loaded<QuotaSnapshot>

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.subheadline.weight(.semibold))
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
}

/// What a point of each Codex window cost over the last 30 days of rollouts.
struct PointCostSection: View {
    let reading: Loaded<[PointCost]>

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("1 point over 30 days").font(.caption).foregroundStyle(.secondary)
            switch reading {
            case .loading:
                Text("Reading…").font(.caption).foregroundStyle(.secondary)
            case .failed(let message):
                FailureText(message: message)
            case .loaded(let costs):
                ForEach(costs, id: \.kind) { cost in
                    HStack {
                        Text(pointCostLabel(cost)).font(.caption)
                        Spacer()
                        Text(pointCostText(cost) ?? "not enough readings")
                            .font(.caption.monospacedDigit())
                    }
                    .help("\(cost.spans) spans between whole-percent crossings, "
                        + "\(Int(cost.points)) points")
                }
            }
        }
    }
}

/// A window's use, its bar and the time left until it resets.
struct WindowRow: View {
    let window: QuotaWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(windowLabel(window)).font(.caption)
                Spacer()
                Text("\(Int(window.usedPercent.rounded()))%")
                    .font(.caption.monospacedDigit().weight(.medium))
            }
            ProgressView(value: min(window.usedPercent, 100), total: 100)
                .tint(color(level(window.usedPercent)))
            if let text = window.resetsAt {
                ResetText(text: text)
            }
        }
    }

    private func color(_ level: Level) -> Color {
        switch level {
        case .comfortable: .green
        case .tight: .orange
        case .critical: .red
        }
    }
}

/// `resets in 1 h 21 min · 02:10`, updated every minute.
struct ResetText: View {
    let text: String

    var body: some View {
        switch Result(catching: { try resetDate(text) }) {
        case .success(let reset):
            TimelineView(.periodic(from: .now, by: 60)) { context in
                Text(
                    "resets in \(countdown(context.date, reset)) · "
                        + reset.formatted(date: .abbreviated, time: .shortened)
                )
                .font(.caption2)
                .foregroundStyle(.secondary)
            }
        case .failure(let error):
            FailureText(message: error.localizedDescription)
        }
    }
}

/// What the governor changed since the last `cimrihook init`.
struct GainSection: View {
    let reading: Loaded<Gain>

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Since cimrihook init").font(.subheadline.weight(.semibold))
            switch reading {
            case .loading:
                Text("Reading…").font(.caption).foregroundStyle(.secondary)
            case .failed(let message):
                FailureText(message: message)
            case .loaded(let gain):
                rows(summarize(gain))
            }
        }
    }

    @ViewBuilder
    private func rows(_ summary: GainSummary) -> some View {
        Row(label: "Period compared", value: String(format: "%.1f days each", summary.days))
        Row(label: "Spend per request", value: percentText(summary.spendPerRequestChange))
        Row(label: "Mean context", value: percentText(summary.meanContextChange))
        Row(
            label: "Spend in requests over 200k",
            value: "\(shareText(summary.largeContextShareBefore)) → "
                + shareText(summary.largeContextShareAfter)
        )
        if let saved = summary.receiptSavedUsd {
            Row(
                label: "Compaction receipt",
                value: String(format: "$%.0f saved ", saved) + percentText(summary.receiptChange)
            )
        }
        Text("API list prices, not a subscription bill. Before/after view, not an A/B test.")
            .font(.caption2)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func shareText(_ share: Double?) -> String {
        guard let share else { return "–" }
        return String(format: "%.0f%%", share * 100)
    }
}

struct Row: View {
    let label: String
    let value: String

    var body: some View {
        HStack {
            Text(label).font(.caption)
            Spacer()
            Text(value).font(.caption.monospacedDigit())
        }
    }
}

struct FailureText: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.caption)
            .foregroundStyle(.red)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
    }
}

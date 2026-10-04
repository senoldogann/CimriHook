import AppKit
import CimriHookBarCore
import SwiftUI

/// The panel that opens from the menu bar: one card per provider, then the gain.
struct PanelView: View {
    let store: PanelStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            ProviderCard(logo: .claude, title: "Claude Code", reading: store.claude) {
                EmptyView()
            }
            ProviderCard(logo: .openAI, title: "Codex", reading: store.codex) {
                PointCostLines(reading: store.codexCost)
            }
            Card { GainSection(reading: store.gain) }
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
                        Text("Resets in \(countdown(context.date, reset))")
                            .help(reset.formatted(date: .abbreviated, time: .shortened))
                    }
                    Spacer()
                    if let heading = try? pace(window, context.date) {
                        Text(paceLabel(heading, context.date))
                            .foregroundStyle(paceColor(heading))
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
    }

    private func paceLabel(_ heading: Pace, _ now: Date) -> String {
        switch heading {
        case .full: "Full"
        case .fillsAt(let date): "Full in \(countdown(now, date))"
        case .lastsUntilReset: "On pace"
        }
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
        if case .loaded(let costs) = reading, !costs.isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(costs, id: \.kind) { cost in
                    if let text = pointCostText(cost) {
                        Text("\(pointCostLabel(cost)) point ≈ \(text)")
                            .help(
                                "Fitted over the last 30 days of Codex rollouts: \(cost.spans) "
                                    + "spans between whole-percent crossings")
                    }
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        } else if case .failed(let message) = reading {
            FailureText(message: message)
        }
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
                content(summarize(gain))
            }
        }
    }

    @ViewBuilder
    private func content(_ summary: GainSummary) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Figure(value: percentText(summary.spendPerRequestChange), label: "Per request")
            Figure(value: percentText(summary.meanContextChange), label: "Context")
            if let saved = summary.receiptSavedUsd {
                Figure(value: String(format: "$%.0f", saved), label: "Saved")
            }
        }
        Text(String(format: "%.1f days before vs after, at API list prices", summary.days))
            .font(.caption2)
            .foregroundStyle(.tertiary)
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

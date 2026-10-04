import AppKit
import CimriHookBarCore
import SwiftUI

/// The menu bar item: each provider's mark and its fullest window, `✳ 46%  ◎ 3%`.
///
/// It is drawn as one template image, so it takes the menu bar's own colour in light and dark
/// mode like the system's items do.
@MainActor
func menuBarImage(_ claude: QuotaSnapshot?, _ codex: QuotaSnapshot?) -> NSImage {
    let renderer = ImageRenderer(
        content: HStack(spacing: 8) {
            MenuBarEntry(logo: .claude, snapshot: claude)
            MenuBarEntry(logo: .openAI, snapshot: codex)
        }
        .frame(height: 18)
    )
    renderer.scale = NSScreen.main?.backingScaleFactor ?? 2
    let image =
        renderer.nsImage
        ?? NSImage(systemSymbolName: "gauge.with.dots.needle.50percent", accessibilityDescription: nil)!
    image.isTemplate = true
    image.accessibilityDescription = "CimriHook usage"
    return image
}

private struct MenuBarEntry: View {
    let logo: ProviderLogo
    let snapshot: QuotaSnapshot?

    var body: some View {
        HStack(spacing: 3) {
            Image(nsImage: logo.image).renderingMode(.template)
                .resizable()
                .frame(width: 13, height: 13)
            Text(fullestPercent(snapshot).map { "\(Int($0.rounded()))%" } ?? "–")
                .font(.system(size: 13, weight: .medium).monospacedDigit())
        }
        .foregroundStyle(.black)
    }
}

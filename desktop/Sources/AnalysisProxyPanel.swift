import SwiftUI

struct AnalysisProxyPanel: View {
  @Binding var resolution: String
  @Binding var maxFPS: Double
  var body: some View {
    Panel {
      VStack(alignment: .leading, spacing: 13) {
        Text("Analysis proxy").font(.system(size: 15, weight: .medium))
        HStack {
          Text("Resolution").foregroundStyle(Palette.muted)
          Spacer()
          Picker("Proxy resolution", selection: $resolution) {
            Text("Original · No resizing").tag("original")
            Text("Up to 1080p").tag("1080p")
            Text("Up to 720p").tag("720p")
          }.labelsHidden().frame(width: 230).accessibilityLabel("Proxy resolution")
        }
        HStack {
          Text("Max frame rate").foregroundStyle(Palette.muted)
          Spacer()
          Picker("Proxy frame rate", selection: $maxFPS) {
            Text("Original").tag(Double(0))
            ForEach([30, 15, 5], id: \.self) { fps in
              Text("Up to \(fps) fps").tag(Double(fps))
            }
          }.labelsHidden().frame(width: 230).accessibilityLabel("Proxy frame rate")
        }
        Text("Temporary smaller video copies for visual analysis and OCR. Playback and speech use the original. Resolution and frame rate are only reduced.")
          .font(.system(size: 12)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
        Text("Creating a proxy takes time and disk space, so short clips may not be faster overall. Lower resolution can lose small text; lower FPS can miss brief events. Proxy FPS is separate from the number of frames the AI analyzes.")
          .font(.system(size: 11)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
      }.font(.system(size: 13)).padding(18)
    }
  }
}

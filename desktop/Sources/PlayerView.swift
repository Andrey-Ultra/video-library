import AVKit
import SwiftUI

struct ClipPlayerView: View {
  @ObservedObject var playback: ClipPlayback
  @State private var scrubPosition: Double?
  @State private var resumeAfterScrub = false
  @FocusState private var timelineFocused: Bool

  private var shownPosition: Double { scrubPosition ?? playback.position }
  var body: some View {
    VStack(spacing: 0) {
      if let player = playback.player {
        NativeVideoSurface(player: player)
          .aspectRatio(16.0 / 9, contentMode: .fit)
          .background(Color.black)
          .overlay(alignment: .center) {
            if !playback.isReady && playback.error == nil { ProgressView().controlSize(.small) }
          }
          .accessibilityLabel("Source video")
      }
      VStack(spacing: 12) {
        timeline
        HStack(spacing: 10) {
          Button {
            playback.toggle()
          } label: {
            Image(systemName: playback.wantsPlayback ? "pause.fill" : "play.fill")
              .frame(width: 25, height: 22)
          }.buttonStyle(.plain).foregroundStyle(Palette.cyan)
            .disabled(!playback.canControl)
            .accessibilityLabel(
              playback.wantsPlayback ? "Pause video" : "Play video")
          Text(clock(shownPosition) + " / " + clock(playback.duration))
            .font(.system(size: 10, design: .monospaced)).foregroundStyle(Palette.muted)
            .monospacedDigit().lineLimit(1)
          Spacer(minLength: 4)
          if playback.isSeeking || playback.isWaiting { ProgressView().controlSize(.mini) }
          Button {
            playback.toggleMute()
          } label: {
            Image(
              systemName: playback.isMuted || playback.volume == 0
                ? "speaker.slash" : "speaker.wave.2"
            )
            .frame(width: 20, height: 22)
          }.buttonStyle(.plain).foregroundStyle(Palette.muted)
            .accessibilityLabel(
              playback.isMuted || playback.volume == 0 ? "Unmute video" : "Mute video")
          Slider(value: $playback.volume, in: 0...1).labelsHidden().frame(width: 62)
            .controlSize(.mini).tint(Palette.cyan).accessibilityLabel("Video volume")
        }
        if let selection = playback.selection {
          HStack(spacing: 5) {
            RoundedRectangle(cornerRadius: 1).fill(Palette.pink).frame(width: 3, height: 10)
            Text(selection.isFrame ? "FRAME" : "SELECTED")
              .font(.system(size: 9, weight: .semibold)).tracking(0.7)
            Text(
              selection.isFrame
                ? clock(selection.start, precise: true)
                : clock(selection.start, precise: true) + " – "
                  + clock(selection.end, precise: true)
            )
            .font(.system(size: 10, design: .monospaced)).monospacedDigit()
            Spacer(minLength: 0)
          }.foregroundStyle(Palette.pink)
        }
        if let error = playback.error {
          Text(error).font(.system(size: 11)).foregroundStyle(Palette.pink)
            .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }
      }.padding(13).background(Palette.panel)
    }.clipShape(RoundedRectangle(cornerRadius: 8))
      .overlay(RoundedRectangle(cornerRadius: 8).stroke(Palette.line))
  }

  var timeline: some View {
    GeometryReader { geometry in
      let width = max(1, geometry.size.width)
      let total = max(0.001, playback.duration)
      let cursor = CGFloat(min(1, max(0, shownPosition / total))) * width
      ZStack(alignment: .leading) {
        RoundedRectangle(cornerRadius: 3).fill(Color.white.opacity(0.13)).frame(height: 4)
        RoundedRectangle(cornerRadius: 3).fill(Palette.cyan.opacity(0.65))
          .frame(width: cursor, height: 4)
        if let selection = playback.selection {
          let start = CGFloat(min(1, max(0, selection.start / total))) * width
          let end = CGFloat(min(1, max(0, selection.end / total))) * width
          let bandWidth = max(3, end - start)
          RoundedRectangle(cornerRadius: 2).fill(Palette.pink.opacity(0.35))
            .frame(width: bandWidth, height: 12).offset(x: min(start, max(0, width - bandWidth)))
          Rectangle().fill(Palette.pink).frame(width: 2, height: 13)
            .offset(x: min(start, width - 2))
          if !selection.isFrame {
            Rectangle().fill(Palette.pink).frame(width: 2, height: 13)
              .offset(x: min(end, width - 2))
          }
        }
        Circle().fill(Palette.cyan).frame(width: 10, height: 10)
          .offset(x: min(max(0, cursor - 5), width - 10))
      }.frame(height: 24).contentShape(Rectangle())
        .gesture(
          DragGesture(minimumDistance: 0).onChanged { value in
            if scrubPosition == nil {
              resumeAfterScrub = playback.wantsPlayback || playback.isPlaying || playback.isWaiting
              playback.pause()
              timelineFocused = true
            }
            scrubPosition = min(max(0, value.location.x / width), 1) * playback.duration
          }.onEnded { value in
            let target = min(max(0, value.location.x / width), 1) * playback.duration
            scrubPosition = nil
            playback.seek(to: target, playAfter: resumeAfterScrub)
          }
        )
        .focusable().focusEffectDisabled().focused($timelineFocused)
        .onKeyPress(.leftArrow) {
          playback.seek(to: playback.position - 5, playAfter: playback.isPlaying)
          return .handled
        }
        .onKeyPress(.rightArrow) {
          playback.seek(to: playback.position + 5, playAfter: playback.isPlaying)
          return .handled
        }
        .onKeyPress(.space) {
          playback.toggle()
          return .handled
        }
        .accessibilityElement().accessibilityLabel("Video timeline")
        .accessibilityValue(clock(shownPosition) + " of " + clock(playback.duration))
        .accessibilityHint(
          "Seek anywhere in the full video. The pink marker shows the selected observation."
        )
        .accessibilityAdjustableAction { direction in
          let delta = direction == .increment ? 5.0 : -5.0
          playback.seek(to: playback.position + delta, playAfter: playback.isPlaying)
        }
        .disabled(!playback.canControl)
    }.frame(height: 24)
  }
}

struct PlaySelectionButton: View {
  @ObservedObject var playback: ClipPlayback
  var body: some View {
    ActionButton(label: "Play selection", symbol: "play.fill", disabled: !playback.canControl) {
      playback.playSelection()
    }.help("Return to the start of the selected observation and play the full source from there")
      .accessibilityLabel("Play selection from start")
  }
}

struct NativeVideoSurface: NSViewRepresentable {
  let player: AVPlayer
  func makeNSView(context: Context) -> AVPlayerView {
    let view = AVPlayerView()
    view.controlsStyle = .none
    view.videoGravity = .resizeAspect
    view.player = player
    return view
  }
  func updateNSView(_ view: AVPlayerView, context: Context) {
    if view.player !== player { view.player = player }
  }
  static func dismantleNSView(_ view: AVPlayerView, coordinator: ()) {
    view.player = nil
  }
}

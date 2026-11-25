import AVFoundation
import Foundation

struct CheckFailure: Error, CustomStringConvertible {
  let description: String
}

@main struct PlaybackChecks {
  @MainActor static func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw CheckFailure(description: message) }
  }
  @MainActor static func wait(_ message: String, timeout: Double = 8, until condition: () -> Bool)
    async throws
  {
    let deadline = Date().addingTimeInterval(timeout)
    while !condition() {
      if Date() > deadline { throw CheckFailure(description: message) }
      try await Task.sleep(for: .milliseconds(20))
    }
  }
  @MainActor static func main() async throws {
    guard CommandLine.arguments.count == 2 else {
      throw CheckFailure(description: "Pass a video of at least 12 seconds")
    }
    let source = URL(fileURLWithPath: CommandLine.arguments[1])
    let playback = ClipPlayback()
    playback.isMuted = true
    // A click before AVPlayerItem is ready must queue the seek, not start at 0.
    playback.open(url: source, durationHint: 12, identity: "first-load")
    playback.setSelection(start: 4, end: 7, isFrame: false)
    playback.playSelection()
    try await wait("Queued selection did not start playback at 4 seconds") {
      playback.isReady && playback.isPlaying && playback.position >= 3.95 && playback.position < 5.5
    }
    playback.pause()
    print("PASS: play before readiness starts at selection")

    // Ordinary scrubbing is allowed outside the highlighted observation.
    playback.seek(to: 10, playAfter: false)
    try await wait("Seeking outside the selection was restricted") {
      !playback.isSeeking && abs(playback.position - 10) < 0.15
    }
    try require(
      playback.selection?.start == 4 && playback.selection?.end == 7,
      "Scrubbing changed selection bounds")
    playback.toggle()
    try await wait("Main play button did not resume the scrubbed position") {
      playback.isPlaying && playback.position >= 9.9
    }
    playback.pause()
    print("PASS: scrub and main play use the whole source")

    // The separate button always restarts the selection, even after scrubbing.
    playback.playSelection()
    try await wait("Play selection resumed at 10 instead of returning to 4") {
      playback.isPlaying && playback.position >= 3.95 && playback.position < 5.5
    }
    try await Task.sleep(for: .milliseconds(250))
    playback.playSelection()
    try await wait("A repeated selection click did not rewind and play") {
      !playback.isSeeking && playback.isPlaying && playback.position >= 3.95
        && playback.position < 4.5
    }
    playback.pause()
    print("PASS: separate play always rewinds the selected observation")

    // Playback continues beyond the pink interval; it is not a trim or loop.
    playback.setSelection(start: 1, end: 1.2, isFrame: false)
    playback.playSelection()
    try await wait("Playback stopped at the highlighted range boundary") {
      playback.isPlaying && playback.position > 1.6
    }
    playback.pause()
    print("PASS: selection end does not stop playback")

    // Cancel an old play-seek with a new, paused selection. Late callbacks lose.
    playback.setSelection(start: 2, end: 3, isFrame: false)
    playback.playSelection()
    playback.setSelection(start: 8, end: 9, isFrame: false)
    try await wait("Latest selection did not win the seek race") {
      !playback.isSeeking && abs(playback.position - 8) < 0.15
    }
    try await Task.sleep(for: .milliseconds(300))
    try require(
      !playback.isPlaying && playback.player?.rate == 0,
      "Cancelled completion restarted old playback")
    try require(playback.error == nil, "Expected seek cancellation became a playback error")
    print("PASS: rapid selection cancels stale autoplay")

    playback.setSelection(start: 3.003, end: 3.004, isFrame: true)
    playback.playSelection()
    try await wait("OCR frame could not start normal playback") {
      playback.isPlaying && playback.position > 3.1
    }
    try require(playback.selection?.isFrame == true, "OCR marker lost its single-frame semantics")
    playback.pause()
    print("PASS: OCR marker plays forward without a 1ms stop")

    // Pause while a seek is pending must suppress its subsequent autoplay.
    playback.playSelection()
    playback.pause()
    try await wait("Pause left a seek stuck") { !playback.isSeeking }
    try await Task.sleep(for: .milliseconds(150))
    try require(!playback.isPlaying, "Pause during seek was ignored")
    playback.clearSelection()
    try require(playback.selection == nil, "Clearing evidence left an old highlight")
    playback.close()
    try require(
      playback.player == nil && !playback.isPlaying && !playback.isReady,
      "Closing left the source playing")
    print("PASS: pause and close release playback")
    print("7 playback checks passed")
  }
}

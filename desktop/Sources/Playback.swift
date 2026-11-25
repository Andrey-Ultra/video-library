import AVFoundation
import Combine
import Foundation

struct PlaybackSelection: Equatable {
  let start: Double
  let end: Double
  let isFrame: Bool
}

/// Owns one source player. Selection is an annotation, never a playback boundary.
@MainActor final class ClipPlayback: ObservableObject {
  @Published private(set) var player: AVPlayer?
  @Published private(set) var position = 0.0
  @Published private(set) var duration = 0.0
  @Published private(set) var isReady = false
  @Published private(set) var isPlaying = false
  @Published private(set) var isWaiting = false
  @Published private(set) var isSeeking = false
  @Published private(set) var selection: PlaybackSelection?
  @Published private(set) var error: String?
  @Published private(set) var wantsPlayback = false
  @Published var volume: Float = 0.7 { didSet { player?.volume = volume } }
  @Published var isMuted = false { didSet { player?.isMuted = isMuted } }
  private var sourceIdentity: String?
  private var timeObserver: Any?
  private var statusObserver: NSKeyValueObservation?
  private var controlObserver: NSKeyValueObservation?
  private var endObserver: NSObjectProtocol?
  private var pendingSeek: (time: Double, play: Bool)?
  private var seekRevision = 0

  var canControl: Bool { player != nil && error == nil && duration > 0 }

  func open(url: URL, durationHint: Double, identity: String) {
    guard sourceIdentity != identity || player == nil else { return }
    close()
    sourceIdentity = identity
    duration = durationHint.isFinite ? max(0, durationHint) : 0
    let item = AVPlayerItem(url: url)
    let source = AVPlayer(playerItem: item)
    source.volume = volume
    source.isMuted = isMuted
    source.actionAtItemEnd = .pause
    player = source
    statusObserver = item.observe(\.status, options: [.initial, .new]) {
      [weak self, weak item] _, _ in
      Task { @MainActor in
        guard let self, let item, self.player?.currentItem === item else { return }
        switch item.status {
        case .readyToPlay:
          let actualDuration = item.duration.seconds
          if actualDuration.isFinite && actualDuration > 0 { self.duration = actualDuration }
          self.isReady = true
          if let pending = self.pendingSeek {
            self.pendingSeek = nil
            self.seek(to: pending.time, playAfter: pending.play)
          }
        case .failed:
          self.error = item.error?.localizedDescription ?? "This source could not be played."
          self.pendingSeek = nil
          self.isSeeking = false
          self.wantsPlayback = false
        default: break
        }
      }
    }
    controlObserver = source.observe(\.timeControlStatus, options: [.initial, .new]) {
      [weak self, weak source] _, _ in
      Task { @MainActor in
        guard let self, let source, self.player === source else { return }
        self.isPlaying = source.timeControlStatus == .playing
        self.isWaiting = source.timeControlStatus == .waitingToPlayAtSpecifiedRate
      }
    }
    timeObserver = source.addPeriodicTimeObserver(
      forInterval: CMTime(seconds: 0.1, preferredTimescale: 600), queue: .main
    ) { [weak self, weak source] time in
      Task { @MainActor in
        guard let self, let source, self.player === source, !self.isSeeking else { return }
        let seconds = time.seconds
        if seconds.isFinite { self.position = self.clamped(seconds) }
      }
    }
    endObserver = NotificationCenter.default.addObserver(
      forName: AVPlayerItem.didPlayToEndTimeNotification, object: item, queue: .main
    ) { [weak self, weak item] _ in
      Task { @MainActor in
        guard let self, let item, self.player?.currentItem === item else { return }
        self.wantsPlayback = false
        self.isPlaying = false
        self.position = self.duration
      }
    }
  }

  func setSelection(start: Double, end: Double, isFrame: Bool) {
    guard start.isFinite, end.isFinite else { return }
    let lower = min(clamped(start), max(0, duration - 0.001))
    selection = PlaybackSelection(start: lower, end: max(lower, clamped(end)), isFrame: isFrame)
    seek(to: lower, playAfter: false)
  }

  func clearSelection() { selection = nil }

  func playSelection() {
    guard let selection else { return }
    seek(to: selection.start, playAfter: true)
  }

  func toggle() {
    if wantsPlayback || isPlaying || isWaiting {
      pause()
    } else {
      let start = position >= duration - 0.001 ? 0 : position
      seek(to: start, playAfter: true)
    }
  }

  func pause() {
    wantsPlayback = false
    if let pending = pendingSeek { pendingSeek = (pending.time, false) }
    player?.pause()
  }

  /// A late completion from a cancelled seek must never start an older selection.
  func seek(to seconds: Double, playAfter: Bool) {
    guard seconds.isFinite, let source = player, error == nil else { return }
    let target = clamped(seconds)
    seekRevision += 1
    let revision = seekRevision
    wantsPlayback = playAfter
    source.pause()
    position = target
    isSeeking = true
    guard isReady else {
      pendingSeek = (target, playAfter)
      return
    }
    pendingSeek = nil
    source.currentItem?.cancelPendingSeeks()
    source.seek(
      to: CMTime(seconds: target, preferredTimescale: 1000),
      toleranceBefore: .zero, toleranceAfter: .zero
    ) { [weak self, weak source] finished in
      Task { @MainActor in
        guard let self, let source, self.player === source, self.seekRevision == revision else {
          return
        }
        self.isSeeking = false
        if finished {
          let actual = source.currentTime().seconds
          self.position = actual.isFinite ? self.clamped(actual) : target
          if playAfter && self.wantsPlayback { source.play() }
        } else {
          self.wantsPlayback = false
          self.error = "Could not move to this time. Reload the clip and try again."
        }
      }
    }
  }

  func toggleMute() {
    if volume == 0 {
      volume = 0.7
      isMuted = false
    } else {
      isMuted.toggle()
    }
  }

  func close() {
    seekRevision += 1
    wantsPlayback = false
    pendingSeek = nil
    player?.pause()
    player?.currentItem?.cancelPendingSeeks()
    if let timeObserver { player?.removeTimeObserver(timeObserver) }
    timeObserver = nil
    statusObserver?.invalidate()
    controlObserver?.invalidate()
    statusObserver = nil
    controlObserver = nil
    if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
    endObserver = nil
    player = nil
    sourceIdentity = nil
    position = 0
    duration = 0
    isReady = false
    isPlaying = false
    isWaiting = false
    isSeeking = false
    selection = nil
    error = nil
  }

  private func clamped(_ seconds: Double) -> Double { min(max(0, seconds), duration) }
}

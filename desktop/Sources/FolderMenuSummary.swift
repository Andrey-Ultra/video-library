import Foundation

/// Completed videos and estimated work are different measures of folder coverage.
struct FolderMenuSummary {
  let folder: String
  let status: String
  let total: Int
  let indexed: Int
  let indexedPercent: Int
  let workPercent: Int?

  init(_ state: Snapshot?) {
    guard let state else {
      folder = "No folder selected"
      status = "Opening library…"
      total = 0
      indexed = 0
      indexedPercent = 0
      workPercent = nil
      return
    }
    folder = state.active?.path ?? "No folder selected"
    total = state.videos.count
    indexed = state.videos.filter { $0.needs_index == false && $0.error == nil }.count
    indexedPercent = total > 0 ? Int((Double(indexed) / Double(total) * 100).rounded()) : 0
    let pending = total - indexed
    let running = state.job?.running == true
    let resumable = ["paused", "interrupted"].contains(state.job?.status ?? "")
    let fraction = state.progress.fraction
    workPercent =
      (running && state.job?.kind == "index") || (resumable && pending > 0)
      ? Int((min(1, max(0, fraction.isFinite ? fraction : 0)) * 100).rounded()) : nil
    if state.folder_unavailable == true {
      status = "Folder unavailable"
    } else if state.active == nil {
      status = "No folder selected"
    } else if running {
      status =
        state.job?.status == "pausing"
        ? "Saving checkpoints…"
        : state.job?.kind == "refresh" ? "Scanning folder…" : "Indexing"
    } else if total == 0 {
      status = state.job?.status == "complete" ? "No videos found" : "Not scanned yet"
    } else if pending == 0 {
      status = "Up to date"
    } else if state.job?.status == "failed"
      || state.videos.contains(where: {
        $0.error != nil || $0.stages.contains(where: { $0.status == "failed" })
      })
    {
      status = "Needs attention"
    } else if resumable {
      status = "Paused · checkpoints saved"
    } else {
      status = "Ready to index"
    }
  }

  var videosText: String { "Videos: \(total)" }
  var indexedText: String { "Indexed: \(indexed) of \(total) (\(indexedPercent)%)" }
  var workText: String? { workPercent.map { "Processing progress: \($0)% (approx.)" } }
}

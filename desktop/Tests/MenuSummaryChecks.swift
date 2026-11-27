import Foundation

@main struct MenuSummaryChecks {
  static func snapshot(_ changes: [String: Any] = [:]) throws -> Snapshot {
    var data: [String: Any] = [
      "libraries": [], "active_id": "test",
      "active": ["id": "test", "path": "/footage/Test library", "config": [:]],
      "videos": [], "file_count": 0, "duplicate_count": 0, "item_count": 0,
      "progress": ["fraction": 0], "mcp_config": [:], "mcp_command": "", "defaults": [:],
    ]
    data.merge(changes) { _, new in new }
    return try JSONDecoder().decode(
      Snapshot.self, from: JSONSerialization.data(withJSONObject: data))
  }

  static func video(_ id: Int, ready: Bool, failed: Bool = false) -> [String: Any] {
    var result: [String: Any] = [
      "id": id, "path": "/footage/\(id).mov", "fingerprint": "hash-\(id)",
      "metadata": [:], "stages": [], "needs_index": !ready,
    ]
    if failed { result["error"] = "Index failed" }
    return result
  }

  static func main() throws {
    let stageProgress = try snapshot(["progress": ["fraction": 0.2, "stage_eta_s": 120,
      "stage_eta_name": "ocr", "stage_eta_status": "estimated"]]).progress
    precondition(stageProgress.remainingLabel == "~2m 0s left · On-screen text")
    let totalProgress = try snapshot(["progress": ["fraction": 0.2, "eta_s": 240,
      "stage_eta_s": 120, "stage_eta_name": "ocr"]]).progress
    precondition(totalProgress.remainingLabel == "~4m 0s remaining")
    let resources = try JSONDecoder().decode(ModelResources.self, from: Data("""
      {"hardware":{"supported":true,"chip":"Apple M4","memory_gb":16,"cpu_cores":10},
       "models":[],"required":[],"recommended":{"visual":{"model":"suggested"},
       "proxy":{"resolution":"original"},"runtime":{"cache_dir":"default"},"answer":{"model":"default"}}}
      """.utf8))
    let original: JSONValue = .object(["proxy": .object(["resolution": .string("720p"), "max_fps": .number(15)]),
      "runtime": .object(["cache_dir": .string("/chosen/cache")]),
      "answer": .object(["model": .string("existing-answer")])])
    let suggested = resources.suggestedConfig(preserving: original)
    precondition(suggested["proxy"] == original["proxy"] && suggested["runtime"] == original["runtime"])
    precondition(suggested["answer"] == original["answer"] && suggested["visual"]["model"].string == "suggested")
    precondition(resources.hardware.description == "Apple M4 · 16 GB memory")
    let catalog = try JSONDecoder().decode(ModelResources.self, from: Data("""
      {"hardware":{"supported":true,"chip":"Apple M4","memory_gb":16,"cpu_cores":10},
       "models":[{"id":"new/model-from-api","section":"visual","name":"New model","detail":"","status":"ready"}],
       "required":[],"recommended":{}}
      """.utf8))
    var draft: JSONValue = .object(["visual": .object(["backend": .string("mlx"), "model": .string("new/model-from-api")]),
      "speech": .object(["backend": .string("none")]), "embedding": .object(["backend": .string("none")])])
    precondition(catalog.customModelSections(in: draft).isEmpty, "New server catalog models need no Swift catalog edit")
    draft["visual"]["model"] = .string("/custom/local/model")
    precondition(catalog.customModelSections(in: draft) == ["visual"])
    precondition(catalog.customModelSections(in: draft).subtracting(["visual"]).isEmpty,
      "A delayed catalog must preserve an explicit processing choice")
    draft["visual"]["model"] = .string("new/model-from-api")
    draft["visual"]["backend"] = .string("openai")
    precondition(catalog.customModelSections(in: draft) == ["visual"], "A managed ID on an API backend is still custom")
    let loading = FolderMenuSummary(nil)
    precondition(loading.status == "Opening library…" && loading.workPercent == nil)
    let empty = FolderMenuSummary(try snapshot())
    precondition(empty.indexedText == "Indexed: 0 of 0 (0%)" && empty.status == "Not scanned yet")
    let scanned = FolderMenuSummary(
      try snapshot([
        "job": ["id": "scan", "status": "complete", "kind": "refresh"]
      ]))
    precondition(scanned.status == "No videos found")
    let videos = [video(1, ready: true), video(2, ready: false), video(3, ready: false)]
    let working = FolderMenuSummary(
      try snapshot([
        "videos": videos, "progress": ["fraction": 0.6],
        "job": ["id": "work", "status": "running", "kind": "index"],
      ]))
    precondition(working.folder == "/footage/Test library" && working.videosText == "Videos: 3")
    precondition(working.indexedText == "Indexed: 1 of 3 (33%)")
    precondition(working.workPercent == 60 && working.status == "Indexing")
    let partial = FolderMenuSummary(
      try snapshot([
        "videos": [video(1, ready: false)], "progress": ["fraction": 0.99],
        "job": ["id": "work", "status": "paused", "kind": "index"],
      ]))
    precondition(partial.indexed == 0 && partial.workPercent == 99)
    precondition(partial.status == "Paused · checkpoints saved")
    let done = FolderMenuSummary(
      try snapshot([
        "videos": [video(1, ready: true)],
        "job": ["id": "old", "status": "failed", "kind": "index"],
      ]))
    precondition(done.indexedPercent == 100 && done.status == "Up to date")
    let failed = FolderMenuSummary(try snapshot(["videos": [video(1, ready: false, failed: true)]]))
    precondition(failed.status == "Needs attention" && failed.indexed == 0)
    let unavailable = FolderMenuSummary(try snapshot(["folder_unavailable": true]))
    precondition(unavailable.status == "Folder unavailable")
    let absent = FolderMenuSummary(try snapshot(["active": NSNull()]))
    precondition(absent.status == "No folder selected")
    for (fraction, expected) in [(-0.5, 0), (1.5, 100)] {
      let clamped = FolderMenuSummary(
        try snapshot([
          "videos": videos, "progress": ["fraction": fraction],
          "job": ["id": "work", "status": "running", "kind": "index"],
        ]))
      precondition(clamped.workPercent == expected)
    }
    print("PASS: folder status, indexed coverage, partial progress, errors and empty libraries")
  }
}

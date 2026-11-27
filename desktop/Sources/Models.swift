import Foundation

indirect enum JSONValue: Codable, Equatable {
  case object([String: JSONValue])
  case array([JSONValue])
  case string(String)
  case number(Double)
  case bool(Bool)
  case null
  init(from decoder: Decoder) throws {
    let c = try decoder.singleValueContainer()
    if c.decodeNil() {
      self = .null
    } else if let v = try? c.decode(Bool.self) {
      self = .bool(v)
    } else if let v = try? c.decode(Double.self) {
      self = .number(v)
    } else if let v = try? c.decode(String.self) {
      self = .string(v)
    } else if let v = try? c.decode([String: JSONValue].self) {
      self = .object(v)
    } else {
      self = .array(try c.decode([JSONValue].self))
    }
  }
  func encode(to encoder: Encoder) throws {
    var c = encoder.singleValueContainer()
    switch self {
    case .object(let v): try c.encode(v)
    case .array(let v): try c.encode(v)
    case .string(let v): try c.encode(v)
    case .number(let v): try c.encode(v)
    case .bool(let v): try c.encode(v)
    case .null: try c.encodeNil()
    }
  }
  subscript(_ key: String) -> JSONValue {
    get {
      if case .object(let v) = self { return v[key] ?? .null }
      return .null
    }
    set {
      var v = object
      v[key] = newValue
      self = .object(v)
    }
  }
  var object: [String: JSONValue] {
    if case .object(let v) = self { return v }
    return [:]
  }
  var string: String {
    if case .string(let v) = self { return v }
    return ""
  }
  var number: Double {
    if case .number(let v) = self { return v }
    return 0
  }
  var bool: Bool {
    if case .bool(let v) = self { return v }
    return false
  }
  var pretty: String {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    return (try? String(decoding: encoder.encode(self), as: UTF8.self)) ?? "{}"
  }
}
struct Library: Equatable, Decodable, Identifiable {
  let id: String
  let path: String
  var config: JSONValue
  var name: String { URL(fileURLWithPath: path).lastPathComponent }
}
struct FolderInspection: Decodable {
  let path: String
  let config: JSONValue
  let has_saved_config: Bool
  let needs_resume: Bool?
  let library_id: String?
}
struct IndexSettingsSession: Identifiable {
  let id = UUID()
  let path: String
  let libraryID: String?
  let config: JSONValue
  let initialSetup: Bool
  var name: String { URL(fileURLWithPath: path).lastPathComponent }
}
struct Stage: Equatable, Decodable, Identifiable {
  let name: String
  let status: String
  let checkpoint: Int
  let elapsed_s: Double
  let error: String?
  let provenance: JSONValue?
  var id: String { name }
}
struct Video: Equatable, Decodable, Identifiable {
  let id: Int
  let path: String
  let fingerprint: String
  let metadata: JSONValue
  let stages: [Stage]
  let error: String?
  let summary: String?
  let needs_index: Bool?
  let outdated: [String]?
  let locations: [String]?
  let item_count: Int?
  let vector_count: Int?
  let counts: [String: Int]?
  let source_available: Bool?
  let size_bytes: Int64?
  var name: String {
    URL(fileURLWithPath: path).lastPathComponent.precomposedStringWithCanonicalMapping
  }
  var duration: Double { metadata["duration_ms"].number / 1000 }
  var status: String {
    if error != nil || stages.contains(where: { $0.status == "failed" }) { return "Attention" }
    if stages.contains(where: { $0.status == "running" }) { return "Indexing" }
    return needs_index == true ? "Pending" : "Indexed"
  }
}
struct IndexResetResult: Decodable {
  let library: Library
  let warnings: [String]
}
struct Job: Equatable, Decodable {
  let id: String
  let status: String
  let kind: String
  let error: String?
  let log: String?
  var running: Bool { ["starting", "running", "pausing"].contains(status) }
}
struct ProgressState: Equatable, Decodable {
  let fraction: Double
  let eta_s: Double?
  let eta_status: String?
  let stage_eta_s: Double?
  let stage_eta_name: String?
  let stage_eta_status: String?
  var remainingLabel: String {
    if let eta_s { return "~\(duration(eta_s)) remaining" }
    if let seconds = stage_eta_s, let stage = stage_eta_name {
      let name = IndexMethod.all.first { $0.id == stage }?.name ?? stage
      return "~\(duration(seconds)) left · \(name)"
    }
    return stage_eta_status == "recalibrating" || eta_status == "recalibrating"
      ? "ETA · recalibrating" : "ETA · measuring current stage"
  }
}
struct BusyJob: Equatable, Decodable {
  let id: String
  let library_id: String
}
struct QueueTask: Equatable, Decodable, Identifiable {
  let id: String
  let library_id: String
  let folder: String
  let kind: String
  let status: String
  let error: String?
  let progress: ProgressState?
  let video_count: Int?
  let indexed_count: Int?
  let current_file: String?
  let current_stage: String?
  var name: String {
    URL(fileURLWithPath: folder).lastPathComponent.precomposedStringWithCanonicalMapping
  }
  var running: Bool { ["starting", "running", "pausing"].contains(status) }
  var waiting: Bool { ["queued", "paused", "interrupted"].contains(status) }
}
struct QueueState: Equatable, Decodable {
  let paused: Bool
  let jobs: [QueueTask]
  let running: QueueTask?
  let waiting_count: Int
  var pendingCount: Int { jobs.filter { $0.waiting || $0.running }.count }
}
struct SpeechModelOption: Equatable, Decodable, Identifiable {
  let id: String
  let name: String
  let languages: [String]
}
struct SpeechLanguageOption: Equatable, Decodable {
  let code: String
  let name: String
}
struct SpeechOptions: Equatable, Decodable {
  let parakeet_models: [SpeechModelOption]
  let whisper_languages: [SpeechLanguageOption]
}
struct Snapshot: Equatable, Decodable {
  let speech_options: SpeechOptions?
  let libraries: [Library]
  let active_id: String?
  let active: Library?
  let videos: [Video]
  let job: Job?
  let busy_job: BusyJob?
  let file_count: Int
  let duplicate_count: Int
  let item_count: Int
  let progress: ProgressState
  let mcp_config: JSONValue
  let mcp_command: String
  let defaults: [String: JSONValue]
  let folder_unavailable: Bool?
  let queue: QueueState?
}
struct Observation: Decodable, Identifiable {
  let id: String
  let video_id: Int
  let kind: String
  let text: String
  let start_s: Double
  let end_s: Double
  let payload: JSONValue
  let children: [String]?
  let parents: [String]?
  let provenance: JSONValue?
  let stage_status: String?
  let speech_and_ocr: [Observation]?
  var shortText: String {
    payload["description"].string.isEmpty ? text : payload["description"].string
  }
}
struct ObservationPage: Decodable {
  let total: Int
  let items: [Observation]
}
struct ClipRoute: Codable, Hashable {
  let libraryID: String
  let videoID: Int
}
struct IndexMethod: Identifiable {
  let id: String
  let name: String
  let symbol: String
  let letter: String
  let backends: [String]
  static let all: [Self] = [
    .init(
      id: "visual", name: "Visual understanding", symbol: "viewfinder", letter: "V",
      backends: ["mlx", "openai", "none"]),
    .init(
      id: "speech", name: "Speech transcription", symbol: "waveform", letter: "S",
      backends: ["parakeet", "faster-whisper", "none"]),
    .init(
      id: "ocr", name: "On-screen text", symbol: "text.viewfinder", letter: "T",
      backends: ["vision", "easyocr", "none"]),
    .init(
      id: "hierarchy", name: "Events & video tree", symbol: "point.3.connected.trianglepath.dotted",
      letter: "H", backends: []),
    .init(
      id: "embedding", name: "Semantic search", symbol: "sparkle.magnifyingglass", letter: "E",
      backends: ["mlx", "sentence-transformers", "none"]),
  ]
  func enabled(_ config: JSONValue) -> Bool {
    id == "hierarchy" ? config[id]["enabled"].bool : config[id]["backend"].string != "none"
  }
  func label(_ config: JSONValue) -> String {
    if !enabled(config) { return "Disabled" }
    if id == "hierarchy" {
      return "Visual events · groups of \(Int(config[id]["group_size"].number))"
    }
    if id == "ocr" {
      return
        "\(config[id]["backend"].string == "vision" ? "Apple Vision" : "EasyOCR") · \(config[id]["sample_fps"].number.formatted()) fps"
    }
    return config[id]["model"].string.components(separatedBy: "/").last ?? ""
  }
}
func clock(_ seconds: Double, precise: Bool = false) -> String {
  let ms = Int((max(0, seconds) * 1000).rounded())
  let s = ms / 1000
  let base = String(format: "%02d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
  return precise ? base + String(format: ".%03d", ms % 1000) : base
}
func duration(_ seconds: Double) -> String {
  let s = Int(max(0, seconds).rounded())
  if s >= 3600 { return "\(s/3600)h \(s/60%60)m" }
  return s >= 60 ? "\(s/60)m \(s%60)s" : "\(s)s"
}


struct LocalModelAsset: Decodable, Identifiable {
  let id: String
  let section: String
  let name: String
  let detail: String
  let status: String
  let error: String?
  let completed_files: Int?
  let total_files: Int?
  let filename: String?
  var busy: Bool { status == "downloading" || status == "checking" }
}
struct MacHardware: Decodable {
  let supported: Bool
  let chip: String
  let memory_gb: Int
  let cpu_cores: Int
  var description: String { "\(chip) · \(memory_gb) GB memory" }
}
struct ModelResources: Decodable {
  let hardware: MacHardware
  let recommended: JSONValue
  let models: [LocalModelAsset]
  let required: [String]
  func suggestedConfig(preserving current: JSONValue) -> JSONValue {
    var result = recommended
    for section in ["runtime", "proxy", "answer"] { result[section] = current[section] }
    return result
  }
}

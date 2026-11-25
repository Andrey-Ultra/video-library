import AVKit
import SwiftUI

@MainActor final class ClipModel: ObservableObject {
  let route: ClipRoute
  @Published var video: Video?
  @Published var observations: [Observation] = []
  @Published var roots: [Observation] = []
  @Published var selected: Observation?
  @Published var total = 0
  @Published var busy = false
  @Published var error: String?
  let playback = ClipPlayback()
  var player: AVPlayer? { playback.player }
  private var requestID = UUID()
  init(route: ClipRoute) { self.route = route }
  var prefix: String { "api/libraries/\(route.libraryID)/videos/\(route.videoID)" }
  func load(_ app: LibraryModel) async {
    do {
      let value: Video = try await app.request(prefix)
      video = value
      if value.source_available == true {
        playback.open(
          url: URL(fileURLWithPath: value.path), durationHint: value.duration,
          identity: value.fingerprint)
      } else {
        playback.close()
      }
      let page: ObservationPage = try await app.query(
        prefix + "/observations", parameters: ["kind": "summary", "limit": "100"])
      roots = page.items
    } catch { self.error = error.localizedDescription }
  }
  func list(_ app: LibraryModel, kind: String, query: String, more: Bool = false) async {
    let request = UUID()
    requestID = request
    busy = true
    let offset = more ? observations.count : 0
    do {
      let page: ObservationPage = try await app.query(
        prefix + "/observations",
        parameters: ["kind": kind, "q": query, "offset": String(offset), "limit": "40"])
      guard requestID == request else { return }
      observations = more ? observations + page.items : page.items
      total = page.total
      busy = false
    } catch {
      guard requestID == request else { return }
      self.error = error.localizedDescription
      busy = false
    }
  }
  func select(_ node: Observation, app: LibraryModel) {
    selected = node
    playback.setSelection(start: node.start_s, end: node.end_s, isFrame: node.kind == "ocr")
    Task {
      do {
        let full: Observation = try await app.request(prefix + "/items/\(node.id)")
        if selected?.id == node.id { selected = full }
      } catch { self.error = error.localizedDescription }
    }
  }
}
struct ClipDetailView: View {
  @EnvironmentObject var app: LibraryModel
  @StateObject var clip: ClipModel
  @State private var tab = "Overview"
  @State private var search = ""
  init(clip: ClipModel) { _clip = StateObject(wrappedValue: clip) }
  var kind: String {
    ["Visual windows": "window", "Speech": "speech", "On-screen text": "ocr"][tab] ?? "event"
  }
  var body: some View {
    VStack(spacing: 0) {
      header
      Picker("Evidence layer", selection: $tab) {
        ForEach(
          [
            "Overview", "Video tree", "Visual windows", "Speech", "On-screen text", "Index details",
          ], id: \.self
        ) { Text($0) }
      }.pickerStyle(.segmented).padding(.horizontal, 24).padding(.vertical, 16)
      if let video = clip.video {
        if tab == "Index details" {
          indexDetails(video)
        } else {
          HSplitView {
            leftPane(video).frame(minWidth: 430, maxWidth: .infinity)
            evidenceInspector(video).frame(minWidth: 330, idealWidth: 410, maxWidth: 500)
          }
        }
      } else {
        Spacer()
        ProgressView("Loading clip…")
        Spacer()
      }
    }.background(Palette.background).foregroundStyle(Color.white.opacity(0.9)).preferredColorScheme(
      .dark
    ).frame(minWidth: 1000, minHeight: 700)
      .task { await clip.load(app) }
      .onChange(of: tab) { _, _ in
        search = ""
        clip.selected = nil
        clip.playback.pause()
        clip.playback.clearSelection()
        Task { await clip.list(app, kind: kind, query: "") }
      }
      .task(id: search) {
        if !["Overview", "Video tree", "Index details"].contains(tab) {
          do { try await Task.sleep(for: .milliseconds(250)) } catch { return }
          await clip.list(app, kind: kind, query: search)
        }
      }
      .onDisappear { clip.playback.pause() }
      .alert(
        "Clip details",
        isPresented: Binding(get: { clip.error != nil }, set: { if !$0 { clip.error = nil } })
      ) {
        Button("OK") { clip.error = nil }
      } message: {
        Text(clip.error ?? "")
      }
  }
  var header: some View {
    HStack(spacing: 16) {
      VLMark(size: 34)
      VStack(alignment: .leading, spacing: 6) {
        Text(clip.video?.name ?? "Clip details").font(.system(size: 21, weight: .semibold))
          .textSelection(.enabled)
        Text(clip.video?.path ?? "").font(.system(size: 11)).foregroundStyle(Palette.muted)
          .textSelection(.enabled)
      }
      Spacer()
      Text("SINGLE CLIP").font(.system(size: 10, weight: .semibold)).tracking(1).foregroundStyle(
        Palette.cyan)
      ActionButton(label: "Reload", symbol: "arrow.clockwise") {
        Task {
          await clip.load(app)
          await clip.list(app, kind: kind, query: search)
        }
      }
    }.padding(24).overlay(alignment: .bottom) { Palette.line.frame(height: 1) }
  }
  @ViewBuilder func leftPane(_ video: Video) -> some View {
    if tab == "Overview" {
      ScrollView {
        VStack(alignment: .leading, spacing: 24) {
          Panel {
            VStack(alignment: .leading, spacing: 14) {
              SectionCaption(title: "Visual summary")
              Text(video.summary ?? "No completed visual summary yet.").font(.system(size: 15))
                .lineSpacing(7).textSelection(.enabled)
            }.padding(22)
          }
          HStack(spacing: 12) {
            metric(duration(video.duration), "Duration")
            metric(
              "\(Int(video.metadata["width"].number)) × \(Int(video.metadata["height"].number))",
              "Resolution")
            metric(
              video.metadata["fps"].number.formatted(.number.precision(.fractionLength(2))),
              "Frames / sec")
          }
          Panel {
            VStack(alignment: .leading, spacing: 15) {
              SectionCaption(title: "Explore this clip")
              ForEach(
                [
                  ("Visual windows", "window"), ("Speech", "speech"), ("On-screen text", "ocr"),
                  ("Video tree", "event"),
                ], id: \.0
              ) { title, key in
                Button {
                  tab = title
                } label: {
                  HStack {
                    Text(title)
                    Spacer()
                    Text("\(video.counts?[key] ?? 0)").monospacedDigit().foregroundStyle(
                      Palette.cyan)
                    Image(systemName: "chevron.right").font(.system(size: 10)).foregroundStyle(
                      Palette.muted)
                  }.padding(.vertical, 7)
                }.buttonStyle(.plain)
              }
            }.padding(22)
          }
          Panel {
            VStack(alignment: .leading, spacing: 13) {
              SectionCaption(title: "Source file")
              if let bytes = video.size_bytes {
                LabeledContent(
                  "File size",
                  value: ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file))
              }
              ForEach(video.locations ?? [video.path], id: \.self) {
                Text($0).font(.system(size: 12)).textSelection(.enabled).foregroundStyle(
                  Palette.muted)
              }
              DisclosureGroup("All media metadata") { JSONBlock(value: video.metadata) }
            }.padding(22)
          }
        }.padding(24)
      }
    } else if tab == "Video tree" {
      ScrollView {
        VStack(alignment: .leading, spacing: 18) {
          SectionCaption(title: "Summary · episodes · events · windows")
          Text(
            "Expand a node to see its children. Select any observation to inspect its evidence and source frame."
          ).font(.system(size: 12)).foregroundStyle(Palette.muted)
          ForEach(clip.roots) { node in TreeNode(node: node, clip: clip).environmentObject(app) }
          if clip.roots.isEmpty {
            Text("A completed visual tree is not available yet.").foregroundStyle(Palette.muted)
          }
        }.padding(24)
      }
    } else {
      VStack(alignment: .leading, spacing: 0) {
        HStack {
          Image(systemName: "magnifyingglass").foregroundStyle(Palette.muted)
          TextField("Find text in this clip’s \(tab.lowercased())", text: $search).textFieldStyle(
            .plain)
          if clip.busy { ProgressView().controlSize(.small) }
        }.padding(12).background(Palette.panel, in: RoundedRectangle(cornerRadius: 6)).padding(
          .horizontal, 24
        ).padding(.top, 8)
        HStack {
          Text("\(clip.total) saved observations")
          Spacer()
          if tab == "On-screen text" { Text("Single-frame samples").foregroundStyle(Palette.cyan) }
        }.font(.system(size: 11)).foregroundStyle(Palette.muted).padding(.horizontal, 24).padding(
          .vertical, 14)
        ScrollView {
          LazyVStack(spacing: 10) {
            ForEach(clip.observations) { node in observationCard(node) }
            if clip.observations.count < clip.total {
              ActionButton(label: "Load more", disabled: clip.busy) {
                Task { await clip.list(app, kind: kind, query: search, more: true) }
              }
            }
            if clip.observations.isEmpty && !clip.busy {
              Text(
                search.isEmpty
                  ? "No completed observations in this layer."
                  : "No matching observations in this clip."
              ).foregroundStyle(Palette.muted).padding(35)
            }
          }.padding(.horizontal, 24).padding(.bottom, 24)
        }
      }
    }
  }
  func metric(_ value: String, _ title: String) -> some View {
    Panel {
      VStack(alignment: .leading, spacing: 7) {
        Text(value).font(.system(size: 17, weight: .medium)).monospacedDigit()
        Text(title).font(.system(size: 11)).foregroundStyle(Palette.muted)
      }.frame(maxWidth: .infinity, alignment: .leading).padding(16)
    }
  }
  func observationCard(_ node: Observation) -> some View {
    Button {
      clip.select(node, app: app)
    } label: {
      VStack(alignment: .leading, spacing: 10) {
        HStack {
          Text(
            node.kind == "ocr"
              ? clock(node.start_s, precise: true)
              : clock(node.start_s, precise: true) + " – " + clock(node.end_s, precise: true)
          ).font(.system(size: 12, weight: .medium, design: .monospaced)).foregroundStyle(
            Palette.cyan)
          Spacer()
          if node.kind == "ocr" {
            Text("SAMPLED FRAME").font(.system(size: 9)).foregroundStyle(Palette.muted)
          }
        }
        Text(node.shortText).font(.system(size: 14)).lineSpacing(5).frame(
          maxWidth: .infinity, alignment: .leading)
        Text(node.id).font(.system(size: 10, design: .monospaced)).foregroundStyle(Palette.muted)
      }.padding(17).frame(maxWidth: .infinity, alignment: .leading).background(
        clip.selected?.id == node.id ? Palette.cyan.opacity(0.065) : Palette.panel,
        in: RoundedRectangle(cornerRadius: 7)
      ).overlay(
        RoundedRectangle(cornerRadius: 7).stroke(
          clip.selected?.id == node.id ? Palette.cyan.opacity(0.45) : Palette.line))
    }.buttonStyle(.plain)
  }
  func evidenceInspector(_ video: Video) -> some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 18) {
        if clip.player != nil {
          ClipPlayerView(playback: clip.playback)
        } else {
          RoundedRectangle(cornerRadius: 7).fill(Palette.panel).frame(height: 180).overlay {
            Text("Source unavailable or changed.\nRefresh the library to enable playback.").font(
              .system(size: 12)
            ).foregroundStyle(Palette.muted).multilineTextAlignment(.center)
          }
        }
        if let node = clip.selected {
          HStack {
            SectionCaption(
              title: node.kind == "ocr" ? "Text on a sampled frame" : "Selected observation")
            Spacer()
            PlaySelectionButton(playback: clip.playback)
          }
          Text(clock(node.start_s, precise: true) + " – " + clock(node.end_s, precise: true)).font(
            .system(size: 13, design: .monospaced)
          ).foregroundStyle(Palette.cyan).textSelection(.enabled)
          Text(node.text).font(.system(size: 14)).lineSpacing(6).textSelection(.enabled)
          Text(node.id).font(.system(size: 11, design: .monospaced)).foregroundStyle(Palette.muted)
            .textSelection(.enabled)
          if !node.payload["objects"].pretty.isEmpty && node.kind == "window" {
            payloadSection(node)
          }
          if let overlaps = node.speech_and_ocr, !overlaps.isEmpty {
            Divider()
            SectionCaption(title: "Overlapping speech & text")
            ForEach(overlaps) { other in
              VStack(alignment: .leading, spacing: 5) {
                Text("\(other.kind.uppercased()) · \(clock(other.start_s,precise:true))").font(
                  .system(size: 10, design: .monospaced)
                ).foregroundStyle(Palette.cyan)
                Text(other.text).font(.system(size: 12)).lineSpacing(4).textSelection(.enabled)
              }.padding(.vertical, 6)
            }
          }
          DisclosureGroup("Full observation data") { JSONBlock(value: node.payload) }
          if let provenance = node.provenance {
            DisclosureGroup("Model & indexing settings") { JSONBlock(value: provenance) }
          }
          if let parents = node.parents, !parents.isEmpty {
            DisclosureGroup("Parent nodes") {
              ForEach(parents, id: \.self) { parent in
                Button(parent) {
                  Task {
                    do {
                      let item: Observation = try await app.request(
                        clip.prefix + "/items/\(parent)")
                      clip.select(item, app: app)
                    } catch { clip.error = error.localizedDescription }
                  }
                }.font(.system(size: 11, design: .monospaced)).padding(.vertical, 4)
              }
            }
          }
        } else {
          SectionCaption(title: "Source preview")
          Text(
            "Select an observation to inspect its exact saved text, model output, timestamps and related evidence."
          ).font(.system(size: 13)).foregroundStyle(Palette.muted).lineSpacing(6)
          Text(
            "Everything in this window belongs to this clip. Switching the active library does not change it."
          ).font(.system(size: 11)).foregroundStyle(Palette.muted).lineSpacing(4)
        }
        Divider()
        Text(
          "Sampled observations can miss brief events. Summaries are approximate; verify important details in the source."
        ).font(.system(size: 11)).foregroundStyle(Palette.muted).lineSpacing(4)
      }.padding(24)
    }.background(Palette.sidebar)
  }
  func payloadSection(_ node: Observation) -> some View {
    VStack(alignment: .leading, spacing: 13) {
      ForEach(
        [
          ("Objects", "objects"), ("Actions", "actions"), ("Setting", "setting"),
          ("Uncertainty", "uncertainty"),
        ], id: \.0
      ) { label, key in
        VStack(alignment: .leading, spacing: 5) {
          SectionCaption(title: label)
          Text(
            node.payload[key].string.isEmpty ? node.payload[key].pretty : node.payload[key].string
          ).font(.system(size: 12)).foregroundStyle(Color.white.opacity(0.75)).textSelection(
            .enabled)
        }
      }
    }
  }
  func indexDetails(_ video: Video) -> some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 18) {
        Text("Index layers & provenance").font(.system(size: 22, weight: .semibold))
        Text("Inspect the exact settings, model and checkpoint used for this clip.").font(
          .system(size: 13)
        ).foregroundStyle(Palette.muted)
        ForEach(video.stages) { stage in
          Panel {
            VStack(alignment: .leading, spacing: 16) {
              HStack {
                Text(IndexMethod.all.first(where: { $0.id == stage.name })?.name ?? stage.name)
                  .font(.system(size: 16, weight: .medium))
                Spacer()
                StatusPill(status: stage.status)
              }
              HStack(spacing: 35) {
                LabeledContent("Checkpoint", value: String(stage.checkpoint))
                LabeledContent("Processing time", value: duration(stage.elapsed_s))
              }.font(.system(size: 12))
              if let error = stage.error {
                Text(error).foregroundStyle(Palette.pink).textSelection(.enabled)
              }
              if let provenance = stage.provenance { JSONBlock(value: provenance) }
            }.padding(22)
          }
        }
      }.padding(24)
    }
  }
}

struct TreeNode: View {
  @EnvironmentObject var app: LibraryModel
  let node: Observation
  @ObservedObject var clip: ClipModel
  @State private var expanded = false
  @State private var children: [Observation] = []
  @State private var loaded = false
  @State private var busy = false
  var body: some View {
    VStack(alignment: .leading, spacing: 8) {
      HStack(alignment: .top, spacing: 9) {
        if node.kind != "window" {
          Button {
            expanded.toggle()
            if expanded && !loaded { load() }
          } label: {
            Image(systemName: expanded ? "chevron.down" : "chevron.right").font(.system(size: 10))
              .frame(width: 18, height: 24)
          }.buttonStyle(.plain).accessibilityLabel("\(expanded ? "Collapse" : "Expand") \(node.id)")
        } else {
          Image(systemName: "film").font(.system(size: 11)).foregroundStyle(Palette.muted).frame(
            width: 18, height: 24)
        }
        Button {
          clip.select(node, app: app)
        } label: {
          VStack(alignment: .leading, spacing: 6) {
            HStack {
              Text(node.kind.uppercased()).foregroundStyle(Palette.pink)
              Text(clock(node.start_s) + " – " + clock(node.end_s)).foregroundStyle(Palette.cyan)
            }.font(.system(size: 10, weight: .medium, design: .monospaced))
            Text(node.shortText).font(.system(size: 13)).lineSpacing(4).frame(
              maxWidth: .infinity, alignment: .leading)
          }.padding(12).background(
            clip.selected?.id == node.id ? Palette.cyan.opacity(0.07) : Palette.panel,
            in: RoundedRectangle(cornerRadius: 6))
        }.buttonStyle(.plain)
      }
      if expanded {
        if busy { ProgressView().controlSize(.small).padding(.leading, 30) }
        ForEach(children) { child in
          AnyView(TreeNode(node: child, clip: clip).environmentObject(app)).padding(.leading, 24)
        }
        if loaded && children.isEmpty {
          Text("No child observations.").font(.system(size: 11)).foregroundStyle(Palette.muted)
            .padding(.leading, 30)
        }
      }
    }
  }
  func load() {
    busy = true
    Task {
      do {
        let page: ObservationPage = try await app.request(clip.prefix + "/children/\(node.id)")
        children = page.items
        loaded = true
        busy = false
      } catch {
        busy = false
        clip.error = error.localizedDescription
      }
    }
  }
}

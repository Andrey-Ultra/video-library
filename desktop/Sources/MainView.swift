import AppKit
import SwiftUI

struct MainView: View {
  @EnvironmentObject var model: LibraryModel
  @State private var filter = ""
  @State private var filterStatus = "All media"
  @State private var activity = false
  var videos: [Video] {
    (model.state?.videos ?? []).filter { v in
      (filter.isEmpty || v.name.localizedCaseInsensitiveContains(filter))
        && (filterStatus == "All media"
          || (filterStatus == "Indexed" ? v.needs_index != true : v.needs_index == true))
    }
  }
  var selected: Video? { model.state?.videos.first { $0.id == model.selectedVideo } }
  var body: some View {
    HStack(spacing: 0) {
      sidebar.frame(width: 214)
      Rectangle().fill(Palette.line).frame(width: 1)
      VStack(spacing: 0) {
        topbar
        ScrollView {
          VStack(alignment: .leading, spacing: 26) {
            heading
            stats
            HStack(alignment: .top, spacing: 20) {
              mediaPanel.frame(maxWidth: .infinity)
              inspector.frame(width: 278)
            }
          }.padding(30)
        }
        QueueDock().environmentObject(model)
      }
    }.background(Palette.background).foregroundStyle(Color.white.opacity(0.9)).preferredColorScheme(
      .dark
    )
    .tint(Palette.accent)
    .overlay {
      if model.state == nil && model.error == nil {
        Palette.background.opacity(0.97)
        VStack(spacing: 16) {
          LibraryMark(size: 54)
          ProgressView("Opening your video library…")
          Text(
            "If macOS asks for folder access, allow Video library to read your project and footage."
          )
          .font(.system(size: 12)).foregroundStyle(Palette.muted)
        }
      }
    }
    .frame(minWidth: 1060, minHeight: 690)
    .sheet(item: $model.indexSettings) { session in
      SettingsView(session: session).environmentObject(model)
    }
    .sheet(isPresented: $model.mcpShown) { MCPView().environmentObject(model) }
    .alert(
      "Video library",
      isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })
    ) {
      Button("OK") { model.error = nil }
    } message: {
      Text(model.error ?? "")
    }
    .task { model.start() }
  }
  var sidebar: some View {
    VStack(alignment: .leading, spacing: 20) {
      HStack(spacing: 11) {
        LibraryMark()
        VStack(alignment: .leading, spacing: 5) {
          Text("Video library").font(.system(size: 15, weight: .semibold))
          SectionCaption(title: "Local workspace")
        }
      }.padding(.bottom, 22)
      HStack {
        SectionCaption(title: "Libraries")
        Spacer()
        Button(action: model.chooseFolder) { Image(systemName: "plus") }.buttonStyle(.plain).help(
          "Add library")
      }
      VStack(spacing: 5) {
        ForEach(model.state?.libraries ?? []) { lib in
          Button {
            model.selectFolder(lib.path)
          } label: {
            HStack(spacing: 11) {
              Image(systemName: "folder").foregroundStyle(
                lib.id == model.state?.active_id ? Palette.accent : Palette.muted)
              VStack(alignment: .leading, spacing: 4) {
                Text(lib.name).font(.system(size: 13, weight: .medium)).lineLimit(1)
                Text(
                  model.queuedTask(lib.id)?.status.capitalized
                    ?? (lib.id == model.state?.active_id ? "Selected library" : "Video folder")
                ).font(
                  .system(size: 11)
                ).foregroundStyle(Palette.muted)
              }
              Spacer(minLength: 0)
            }.padding(11).frame(maxWidth: .infinity, alignment: .leading).background(
              lib.id == model.state?.active_id ? Palette.accent.opacity(0.075) : Color.clear,
              in: RoundedRectangle(cornerRadius: 6))
              .contentShape(Rectangle())
          }.buttonStyle(.plain).help(lib.path)
            .contextMenu {
              Button("Indexing settings…", systemImage: "slider.horizontal.3") {
                model.editIndexSettings(lib)
              }.disabled(model.queuedTask(lib.id)?.running == true)
              Divider()
              Button("Delete index…", systemImage: "arrow.counterclockwise", role: .destructive) {
                model.confirmDeleteIndex(lib)
              }.disabled(model.queuedTask(lib.id)?.running == true)
              Button("Delete library and index…", systemImage: "trash", role: .destructive) {
                model.confirmDeleteLibrary(lib)
              }.disabled(model.queuedTask(lib.id)?.running == true)
            }
        }
        Button(action: model.chooseFolder) {
          Label("Add folder", systemImage: "plus").font(.system(size: 13)).foregroundStyle(
            Palette.muted
          ).frame(maxWidth: .infinity, alignment: .leading).padding(11)
            .contentShape(Rectangle())
        }.buttonStyle(.plain)
      }
      Spacer()
      Label("Stored on this Mac", systemImage: "externaldrive").font(.system(size: 11))
        .foregroundStyle(Palette.muted)
      Button {
        model.mcpShown = true
      } label: {
        HStack(spacing: 9) {
          Image(systemName: "point.3.connected.trianglepath.dotted").font(.system(size: 22))
            .foregroundStyle(Palette.accent)
          VStack(alignment: .leading, spacing: 5) {
            Text("Connect your agent").font(.system(size: 12, weight: .medium))
            Text("MCP · active library").font(.system(size: 10)).foregroundStyle(Palette.muted)
          }
          Spacer(minLength: 0)
        }.padding(13).background(Color.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 7))
      }.buttonStyle(.plain)
    }.padding(18).background(Palette.sidebar)
  }
  var topbar: some View {
    HStack {
      Text(model.state?.active?.path ?? "Choose a folder to begin")
        .foregroundStyle(Palette.muted).textSelection(.enabled)
        .fixedSize(horizontal: false, vertical: true)
      Spacer()
    }.font(.system(size: 12)).padding(.horizontal, 30).padding(.vertical, 14)
  }
  var heading: some View {
    HStack(alignment: .center) {
      VStack(alignment: .leading, spacing: 9) {
        SectionCaption(title: "Your footage, understood")
        Text(model.state?.active?.name ?? "Video library").font(
          .system(size: 29, weight: .semibold))
        Text(model.state?.active?.path ?? "Choose a folder to begin").font(.system(size: 12))
          .foregroundStyle(Palette.muted).textSelection(.enabled).lineLimit(2)
      }
      Spacer()
      ActionButton(
        label: model.queuedTask(model.state?.active_id ?? "")?.status.capitalized
          ?? ((model.state?.videos.contains { $0.needs_index == true } ?? false)
            ? "Add to queue" : "Refresh folder"),
        symbol: "arrow.clockwise",
        disabled: model.queuedTask(model.state?.active_id ?? "") != nil
          || model.state?.active == nil
      ) { model.run() }
    }
  }
  var stats: some View {
    HStack(spacing: 40) {
      stat("\(model.state?.videos.count ?? 0)", "Unique videos")
      stat(duration(model.state?.videos.reduce(0, { $0 + $1.duration }) ?? 0), "Total footage")
      stat((model.state?.item_count ?? 0).formatted(), "Indexed observations")
      Spacer()
    }.padding(.bottom, 23).overlay(alignment: .bottom) { Palette.line.frame(height: 1) }
  }
  func stat(_ value: String, _ label: String) -> some View {
    HStack(alignment: .firstTextBaseline, spacing: 9) {
      Text(value).font(.system(size: 23, weight: .medium)).monospacedDigit()
      Text(label).font(.system(size: 12)).foregroundStyle(Palette.muted)
    }
  }
  var mediaPanel: some View {
    Panel {
      VStack(spacing: 0) {
        HStack(spacing: 22) {
          Button {
            activity = false
          } label: {
            Text("Media  \(model.state?.videos.count ?? 0)").foregroundStyle(
              activity ? Palette.muted : Color.white)
          }.buttonStyle(.plain)
          Button {
            activity = true
          } label: {
            Text("Activity").foregroundStyle(activity ? Color.white : Palette.muted)
          }.buttonStyle(.plain)
          Spacer()
          Text(model.state?.job?.running == true ? "Working locally" : "Saved locally").font(
            .system(size: 11)
          ).foregroundStyle(Palette.muted)
        }.font(.system(size: 13, weight: .medium)).padding(18).overlay(alignment: .bottom) {
          Palette.line.frame(height: 1)
        }
        if activity {
          ScrollView {
            Text(model.state?.job?.log ?? "No jobs yet. Refresh your folder or start indexing.")
              .font(.system(size: 11, design: .monospaced)).textSelection(.enabled).frame(
                maxWidth: .infinity, alignment: .leading
              ).padding(18)
          }.frame(minHeight: 300, maxHeight: 460)
        } else {
          HStack {
            HStack {
              Image(systemName: "magnifyingglass").foregroundStyle(Palette.muted)
              TextField("Filter by filename", text: $filter).textFieldStyle(.plain)
            }.padding(9).background(Palette.background, in: RoundedRectangle(cornerRadius: 5))
            Picker("Status", selection: $filterStatus) {
              ForEach(["All media", "Needs indexing", "Indexed"], id: \.self) { Text($0) }
            }.labelsHidden().frame(width: 135)
          }.padding(16)
          HStack {
            SectionCaption(title: "Clip")
            Spacer()
            SectionCaption(title: "Index layers").frame(width: 130)
            SectionCaption(title: "Status").frame(width: 76, alignment: .trailing)
          }.padding(.horizontal, 18).padding(.bottom, 10)
          if videos.isEmpty {
            VStack(spacing: 14) {
              Image(systemName: "film.stack").font(.system(size: 32)).foregroundStyle(
                Palette.accent)
              Text(
                model.state?.folder_unavailable == true
                  ? "Folder unavailable" : "Your footage starts here"
              ).font(.system(size: 18))
              Text(
                filter.isEmpty
                  ? "Choose a folder, then index your footage." : "No clips match this filter."
              ).font(.system(size: 13)).foregroundStyle(Palette.muted)
              ActionButton(label: "Choose folder", primary: true, action: model.chooseFolder)
            }.padding(40).frame(maxWidth: .infinity)
          }
          LazyVStack(spacing: 0) { ForEach(videos) { video in videoRow(video) } }
          HStack {
            Text(
              "\(model.state?.file_count ?? 0) files · \(model.state?.duplicate_count ?? 0) duplicates"
            )
            Spacer()
            Text("Double-click a clip to explore").foregroundStyle(Palette.accent.opacity(0.8))
          }.font(.system(size: 10)).foregroundStyle(Palette.muted).padding(14).overlay(
            alignment: .top
          ) { Palette.line.frame(height: 1) }
        }
      }
    }
  }
  func videoRow(_ video: Video) -> some View {
    HStack(spacing: 13) {
      AsyncImage(url: model.thumbnail(video)) { image in
        image.resizable().scaledToFill()
      } placeholder: {
        Rectangle().fill(Color.white.opacity(0.05)).overlay {
          Image(systemName: "film").foregroundStyle(Palette.muted)
        }
      }.frame(width: 86, height: 58).clipped().cornerRadius(4)
      VStack(alignment: .leading, spacing: 6) {
        Text(video.name).font(.system(size: 13, weight: .medium)).lineLimit(2)
        Text(
          "\(Int(video.metadata["width"].number)) × \(Int(video.metadata["height"].number)) · \(duration(video.duration))"
        ).font(.system(size: 11)).foregroundStyle(Palette.muted)
      }.frame(maxWidth: .infinity, alignment: .leading)
      HStack(spacing: 4) {
        ForEach(IndexMethod.all) { m in
          let done =
            video.stages.first(where: { $0.name == m.id })?.status == "complete"
            && !(video.outdated ?? []).contains(m.id)
          Text(m.letter).font(.system(size: 10, weight: .semibold)).frame(width: 21, height: 24)
            .foregroundStyle(done ? Palette.accent : Palette.muted).background(
              done ? Palette.accent.opacity(0.08) : Color.clear,
              in: RoundedRectangle(cornerRadius: 4)
            ).overlay {
              RoundedRectangle(cornerRadius: 4).stroke(
                done ? Palette.accent.opacity(0.25) : Palette.line)
            }.help(m.name)
        }
      }.frame(width: 130)
      StatusPill(status: video.status).frame(width: 76, alignment: .trailing)
    }.padding(15).background(
      model.selectedVideo == video.id ? Palette.accent.opacity(0.035) : Color.clear
    ).overlay(alignment: .top) { Palette.line.frame(height: 1) }
      .contentShape(Rectangle())
      .gesture(
        TapGesture(count: 2).exclusively(before: TapGesture()).onEnded { gesture in
          switch gesture {
          case .first: openClip(video)
          case .second: model.selectedVideo = video.id
          }
        }
      )
      .accessibilityElement(children: .combine).accessibilityLabel("\(video.name), \(video.status)")
      .accessibilityAddTraits(.isButton)
      .accessibilityAction { model.selectedVideo = video.id }.accessibilityAction(
        named: Text("Open clip details")
      ) { openClip(video) }
  }
  func openClip(_ video: Video) {
    guard let libraryID = model.state?.active_id else { return }
    model.openClip(ClipRoute(libraryID: libraryID, videoID: video.id), title: video.name)
  }
  func settingSummary(_ title: String, _ value: String) -> some View {
    HStack(alignment: .top) {
      Text(title).foregroundStyle(Palette.muted)
      Spacer(minLength: 8)
      Text(value).multilineTextAlignment(.trailing)
    }.font(.system(size: 11))
  }
  var inspector: some View {
    Panel {
      VStack(alignment: .leading, spacing: 16) {
        HStack {
          Text(selected == nil ? "Index coverage" : "Clip details").font(
            .system(size: 14, weight: .semibold))
          Spacer()
          if selected != nil {
            Button {
              model.selectedVideo = nil
            } label: {
              Image(systemName: "xmark")
            }.buttonStyle(.plain)
          } else {
            SectionCaption(title: "Saved")
          }
        }
        if let video = selected {
          Text(video.name).font(.system(size: 14, weight: .medium)).textSelection(.enabled)
          ForEach(video.stages) { stage in
            HStack {
              Text(IndexMethod.all.first(where: { $0.id == stage.name })?.name ?? stage.name).font(
                .system(size: 11))
              Spacer()
              StatusPill(status: stage.status)
            }
          }
          if let summary = video.summary {
            Text(summary).font(.system(size: 12)).lineSpacing(5).foregroundStyle(
              Color.white.opacity(0.7)
            ).textSelection(.enabled)
          }
          ActionButton(
            label: "Explore this clip", symbol: "arrow.up.left.and.arrow.down.right", primary: true
          ) { openClip(video) }
        } else if let config = model.state?.active?.config {
          Text("Saved progress and selected settings.").font(.system(size: 12)).foregroundStyle(Palette.muted)
          VStack(spacing: 9) {
            settingSummary("Analysis source", config["proxy"]["resolution"].string == "1080p" ? "Up to 1080p"
              : config["proxy"]["resolution"].string == "720p" ? "Up to 720p" : "Original")
            settingSummary("Frame rate cap", config["proxy"]["max_fps"].number > 0
              ? "\(Int(config["proxy"]["max_fps"].number)) fps" : "Original")
            settingSummary("Visual sampling", "\(Int(config["visual"]["frames_per_window"].number)) frames / \(config["visual"]["window_s"].number.formatted())s")
            settingSummary("Text sampling", "\(config["ocr"]["sample_fps"].number.formatted()) fps")
          }.padding(.vertical, 4)
          ForEach(IndexMethod.all) { method in
            HStack(spacing: 10) {
              Image(systemName: method.symbol).font(.system(size: 15)).foregroundStyle(
                Palette.accent
              ).frame(width: 32, height: 32).background(
                Color.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 6))
              VStack(alignment: .leading, spacing: 5) {
                Text(method.name).font(.system(size: 12, weight: .medium))
                  .fixedSize(horizontal: false, vertical: true)
                let complete = model.state?.videos.filter { video in
                  video.outdated?.contains(method.id) != true
                    && video.stages.contains { $0.name == method.id && $0.status == "complete" }
                }.count ?? 0
                Text(method.label(config)).font(.system(size: 10)).foregroundStyle(Palette.muted)
                  .lineLimit(2).help(method.label(config))
                Text(method.enabled(config)
                  ? "\(complete) of \(model.state?.videos.count ?? 0) videos ready" : "Disabled")
                  .font(.system(size: 10)).foregroundStyle(Palette.muted).lineLimit(1)
              }.frame(maxWidth: .infinity, alignment: .leading).layoutPriority(1)

            }.padding(.vertical, 8).overlay(alignment: .top) { Palette.line.frame(height: 1) }
          }
          if let library = model.state?.active {
            ActionButton(label: "Indexing settings…", symbol: "slider.horizontal.3") {
              model.editIndexSettings(library)
            }.disabled(model.queuedTask(library.id)?.running == true)
          }
          Divider()
          Label("Made for local processing", systemImage: "memorychip").font(.system(size: 11))
            .foregroundStyle(Palette.accent)
          Text(model.queuedTask(model.state?.active_id ?? "")?.running == true
            ? "Pause this library’s task to change its settings. Completed steps are saved automatically."
            : "Completed steps are saved automatically.").font(
            .system(size: 11)
          ).foregroundStyle(Palette.muted).lineSpacing(4)
        }
      }.padding(18)
    }
  }
}

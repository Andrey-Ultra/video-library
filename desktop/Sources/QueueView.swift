import SwiftUI

struct QueueDock: View {
  @EnvironmentObject var model: LibraryModel
  var queue: QueueState? { model.state?.queue }
  var running: QueueTask? { queue?.running }
  var body: some View {
    VStack(spacing: 0) {
      if model.queueExpanded { taskList }
      HStack(spacing: 20) {
        Image(
          systemName: running != nil
            ? "circle.dotted" : queue?.paused == true ? "pause.circle" : "tray"
        )
        .font(.system(size: 25)).foregroundStyle(Palette.accent)
        VStack(alignment: .leading, spacing: 5) {
          Text(
            running.map { ($0.kind == "refresh" ? "Scanning · " : "Indexing · ") + $0.name }
              ?? (queue?.paused == true
                ? "Queue paused"
                : queue?.pendingCount ?? 0 > 0 ? "Waiting for model memory" : "No tasks running")
          )
          .font(.system(size: 13, weight: .medium)).lineLimit(1)
          if let task = running {
            let stage = IndexMethod.all.first { $0.id == task.current_stage }?.name
            Text(
              task.status == "pausing"
                ? "Saving checkpoints…"
                : (stage ?? "Preparing task…")
                  + (task.current_file.map { " · " + URL(fileURLWithPath: $0).lastPathComponent }
                    ?? "")
            )
            .font(.system(size: 11)).foregroundStyle(Palette.accent).lineLimit(1).truncationMode(
              .middle)
          } else {
            Text(
              queue?.pendingCount ?? 0 > 0
                ? "Tasks and checkpoints are saved." : "Queue empty · browse any library"
            )
            .font(.system(size: 11)).foregroundStyle(Palette.muted)
          }
        }.frame(minWidth: 220, maxWidth: 360, alignment: .leading)
        VStack(alignment: .leading, spacing: 8) {
          HStack {
            Text(running?.progress.map { "\(Int($0.fraction * 100))% · layer coverage" } ?? "")
            Spacer()
            Text(
              running?.progress?.eta_s.map { "~\(duration($0)) remaining" }
                ?? (running == nil
                  ? ""
                  : running?.progress?.eta_status == "recalibrating"
                    ? "ETA · recalibrating" : "ETA · calibrating"))
          }.font(.system(size: 10)).foregroundStyle(Palette.muted)
          ProgressView(value: running?.progress?.fraction ?? 0).tint(Palette.accent)
            .accessibilityLabel("Current task progress")
          Text(
            running.map { "\($0.indexed_count ?? 0) of \($0.video_count ?? 0) videos indexed" }
              ?? "\(queue?.pendingCount ?? 0) tasks pending"
          )
          .font(.system(size: 10)).foregroundStyle(Palette.muted)
        }.frame(maxWidth: .infinity)
        ActionButton(
          label: "Queue · \(queue?.pendingCount ?? 0)",
          symbol: model.queueExpanded ? "chevron.down" : "chevron.up"
        ) {
          model.queueExpanded.toggle()
        }
        if queue?.paused == true {
          ActionButton(
            label: "Resume queue", primary: true,
            disabled: running?.status == "pausing" || queue?.pendingCount == 0
          ) {
            model.queueControl("resume")
          }
        } else if running != nil || (queue?.pendingCount ?? 0) > 0 {
          ActionButton(label: "Pause queue", action: model.pause)
        }
      }.padding(.horizontal, 30).padding(.vertical, 18)
    }.background(Palette.panel).overlay(alignment: .top) { Palette.line.frame(height: 1) }
  }

  var taskList: some View {
    VStack(alignment: .leading, spacing: 10) {
      HStack {
        SectionCaption(title: "Processing queue")
        Spacer()
        Text(
          running?.status == "pausing"
            ? "Saving checkpoints · reordering available after pause"
            : "One task at a time · settings saved with each task"
        ).font(.system(size: 10))
          .foregroundStyle(Palette.muted)
      }
      if queue?.jobs.isEmpty != false {
        Text("Add a library to the queue using the button above its media list.")
          .font(.system(size: 12)).foregroundStyle(Palette.muted).padding(.vertical, 20)
      } else {
        ScrollView {
          LazyVStack(spacing: 0) {
            ForEach(queue?.jobs ?? []) { task in taskRow(task) }
          }
        }.frame(height: 180)
      }
    }.padding(.horizontal, 30).padding(.vertical, 16).background(Palette.sidebar)
      .overlay(alignment: .bottom) { Palette.line.frame(height: 1) }
  }

  func taskRow(_ task: QueueTask) -> some View {
    HStack(spacing: 12) {
      Image(systemName: task.running ? "circle.dotted" : "folder")
        .foregroundStyle(task.running ? Palette.accent : Palette.muted)
      Button {
        model.selectFolder(task.folder)
      } label: {
        VStack(alignment: .leading, spacing: 4) {
          Text(task.name).font(.system(size: 12, weight: .medium)).lineLimit(1)
          Text(task.error ?? (task.kind == "refresh" ? "Scan folder" : "Index library"))
            .font(.system(size: 10)).foregroundStyle(Palette.muted).lineLimit(1)
        }.frame(maxWidth: .infinity, alignment: .leading)
      }.buttonStyle(.plain).help(task.error ?? task.folder)
      StatusPill(status: task.status)
      let waiting = queue?.jobs.filter { $0.waiting } ?? []
      if task.waiting && waiting.count > 1 {
        rowButton("arrow.up", "Move \(task.name) up", disabled: waiting.first?.id == task.id) {
          model.queueAction(task, "move", direction: "up")
        }
        rowButton("arrow.down", "Move \(task.name) down", disabled: waiting.last?.id == task.id) {
          model.queueAction(task, "move", direction: "down")
        }
      }
      if task.waiting {
        rowButton("xmark", "Remove \(task.name) from queue") { model.queueAction(task, "remove") }
      }
    }.padding(.vertical, 9).overlay(alignment: .bottom) { Palette.line.frame(height: 1) }
  }

  func rowButton(
    _ symbol: String, _ label: String, disabled: Bool = false, action: @escaping () -> Void
  ) -> some View {
    Button(action: action) { Image(systemName: symbol).frame(width: 24, height: 22) }
      .buttonStyle(.plain).disabled(disabled).help(label).accessibilityLabel(label)
  }
}

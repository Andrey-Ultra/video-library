# Validation on Mac mini M4 / 16 GB

Recorded on November 25, 2026. Python 3.12.13. Exact library versions are listed in
`requirements-mac.lock.txt`. Models were downloaded in advance; downloading weights
is not included in the measurements. Metal GPU and Apple Vision were available in
a regular macOS session.

| Profile | Material | All stages, wall time | Processing time / footage duration |
|---|---|---:|---:|
| `fast` / default 2B | 60 s of gameplay without audio + 5.759 s of English test speech on a white background | 52.49 s | 0.798 |
| `quality`, 4B | The same 60-second gameplay clip without audio | 75.12 s | 1.252 |

The first test was a complete rerun with `--force`, rather than a resume or a skip
of completed stages. It processed speech, OCR, visual windows, hierarchy and
embeddings. The 4B test video had no audio, so ASR did not run.
The profiles cannot be compared as identical datasets, although the visual source
was the same. Both used 10 s windows, an 8 s step, 6 frames per window, a 448 px
long-side limit, OCR at 1 frame/s, Qwen3-Embedding 0.6B 4-bit, and groups of
8 observations.

Fast-profile stages (seconds, rounded):

| File | Speech | OCR | Descriptions | Hierarchy | Embeddings |
|---|---:|---:|---:|---:|---:|
| English test | 2.26 | 0.46 | 6.79 | 3.41 | 0.98 |
| Gameplay | 0 | 5.65 | 28.49 | 3.68 | 0.50 |

Wall time also includes opening files, creating backend objects, releasing models
and accessing the index. These results describe one short dataset on this computer,
not guaranteed throughput for every archive. The first Apple Vision request had
an additional delay of about 28 seconds; system OCR was already warm in the full
measurement above.

Result checks:

- The English test recognized the supplied phrase about a fox, bird and camera, with timestamps.
- The visual summary of that test described the white background instead of treating
  spoken animal names as animals visible in the frame.
- Hybrid search returned actual gameplay windows, events and a summary.
- The local model answered in Russian with evidence IDs. The CLI added the cited
  nodes' exact time ranges from the database rather than asking the model to generate timestamps.
- Reindexing the completed dataset took about 0.1 s and created no duplicates.
- A real MCP stdio connection and tool discovery were verified.

These measurements used temporary copies in `.cache/benchmark` and
`.cache/benchmark-4b`; the copies were removed during project cleanup. Source videos
were unchanged. The permanent folder for validation against real sources became
`videos/`; videos, indexes and model weights are excluded from Git.

This measurement does not establish rare-event recall over 20 hours of recording,
animal-species recognition quality, or compatibility with every audio/video
container variant. The NVIDIA profile was not tested in this environment.

## Real sources from `videos/`

Three additional user files were processed completely, without transcoding or
modifying the originals:

| File | Duration | Windows | Events | OCR observations | Speech segments |
|---|---:|---:|---:|---:|---:|
| `clip_1min_2026-10-03_21-49-13.mp4` | 01:01.739 | 8 | 1 | 10 | 0 |
| `new-england.mp4` | 38:54.859 | 292 | 111 | 155 | 150 |
| `красивый ролик сна.mov` | 02:59.046 | 23 | 4 | 91 | 16 |

Total: **42:55.644 of video**, **323 windows**, **116 events**, 16 larger episode
nodes, 3 root summaries and **880 embeddings**. Every stage had status `complete`.
The audit checked window coverage, time bounds, reachability of every observation
through the tree, vector compatibility/norms, and unchanged source files.

Total active time across three indexer runs, including failed OCR passes and
recovery: **2256.32 s = 37:36.32**, a processing-to-duration ratio of **0.876**.
This is the sum of the indexer processes' running times, excluding development
pauses between runs. The last pass resumed from checkpoints, so its individual
254.18 s must not be presented as the initial indexing time of the entire archive.

Speed depended substantially on the source: a one-minute 4K/60 fps file required
about 217 s across its stages. Reading/decoding it was noticeably slower than in
the earlier test with a smaller copy. The 1:1 target was reached on average for
this dataset, not for every individual format.

Reference searches were checked against source frames:

- Splitting a log with an orange tool: `new-england.mp4`, **04:56–05:06**.
- Washing a blue boat: the same file, **19:28–19:38**.
- Riding a motorcycle at night: the `.mov` file, including **00:00–00:10**.
- The line `You ever miss the army?`: `.mov`, **01:28.800–01:30.640**.
- The English title `PREPARING FOR THE COLD`: `new-england.mp4`, around **00:19**;
  the OCR result was compared with the frame.

A real MCP connection was checked: an overview of the three files, expanding the
tree, and retrieving a frame at 19:30. Rerunning the completed folder took about
0.13 s and created no duplicates.

Issues found and fixed: accumulated broken native Apple Vision state (OCR was
isolated in a process that is recycled after 400 frames), different Unicode
representations of Russian filenames on macOS, and repeated OCR results crowding
speech and visual observations out of the answer context.

The 2B descriptions remain approximate: for a window containing several shots,
the model may describe only part of what happens or misidentify an object. Five
reference queries confirm specific findings, not overall recall or accuracy across
the archive. Timestamps and source frames remain essential evidence.

Detailed local results: `.cache/real-validation/index-check.json`,
`search-checks.json` and `run.json`. These artifacts and the videos are excluded
from Git.

For free-form answers over long contexts, the visual Qwen3-VL-4B model proved
unreliable: repeated text, language switches and contradictory wording were
observed. Answering was therefore moved to a separate text model,
**Qwen3-4B-Instruct-2507 4-bit**, through MLX-LM. This does not change or recompute
the video index.

In the final check, the answer about washing the boat cited **19:28–19:38**. A
question about the `.mov` file using its usual Russian filename selected the
correct file and distinguished visual content, English speech and on-screen text.
Allowed source IDs are validated; time ranges come from the database. Separate
rules reject repetitive answers and some unsupported claims that events are
absent from an entire video. These checks address specific failures; they do not
guarantee that every statement from a small local model is factually correct.

Final validation: **41 automated tests**, five reference searches, two questions
in Russian and a real MCP connection. Answer text was saved in
`.cache/real-validation/answer-kayak.txt` and `answer-game.txt`.

## App and synchronization validation — 2026-11-25

Refreshing the source folder through the UI preserved 3 video IDs, all 15 completed
stages and 880 observations: no files were added, changed or removed, and models
did not run again. The source database and files passed the audit after migration.

A separate 19:28–19:46 test copy was made from `new-england.mp4`: 640×360,
12 fps, 18 seconds with audio. Running all modules through the UI finished in
**73.17 s**: speech 2.39 s; OCR 56.72 s; visual windows 9.48 s; tree 3.51 s;
embeddings 0.85 s. It produced 2 windows, 2 events, 1 summary, 1 speech segment,
0 nonempty OCR observations and 6 vectors. The first window's description included
washing a blue kayak. Apple Vision needed an automatic restart and a retry of the
same frame. This short run demonstrates cold-start overhead; it does not establish
1:1 processing speed. Original videos were unchanged.

Separate integration tests cover stopping a real worker process after a saved
window, recovery after SIGKILL, Pause/Resume, persistence of settings and the
active folder, a full stdio connection with folder switching without restarting
MCP, renaming and swapping filenames, duplicates, deletion of the last copy, a
sampled-hash collision, migration, and a directory read failure. Failure tests use
deterministic model substitutes; the native run above used real
MLX/Parakeet/Apple Vision. Integer-valued parameters (`10.0` → `10` when serialized
by the browser as JSON) are normalized so saving the form does not rebuild
unchanged stages.

Artifacts: `.cache/ui-validation/app-run.json`, `original-index-check.json`,
`fixture-index-check.json`; the test copy and its index are separate from the
user's `videos` folder.

Final app validation: **57 tests passed in 31.80 s**; Ruff, Python formatting,
JavaScript and launcher syntax, and `git diff --check` passed. The UI was checked
in a compact browser panel: choosing a folder, saving 0.5 fps and restoring it
after restart, returning to the main library, refreshing without running models
again, and copying the MCP command to the clipboard. Another refresh of the
completed archive through the UI took **0.02 s**; this was a warm skip of completed
stages, not the speed of new indexing. At the end, only the user's `videos`
library was active, with all 3 videos and 880 observations preserved.

## Native macOS client — 2026-11-25

The browser frontend was replaced with the SwiftUI/AppKit application
`Video Library.app`. Compilation for Apple Silicon and local signing were
verified. It uses a real `AVPlayerView` with explicit AVKit linkage; validation
resolved a SwiftUI VideoPlayer runtime failure caused by missing ObjC superclass
metadata. The client starts its internal API on a random loopback port and stops
it on exit. Heavy indexing remains a separate process.

**58 Python tests passed in 32.16 s**. A new integration test checks that details,
trees, search and original OCR payloads stay within the selected library ID and
video ID even after the active folder changes. Requests for another video's item
ID are rejected; incomplete layers are not returned as published observations.

For the real `красивый ролик сна.mov`, the detailed API confirmed 23 visual
windows, 4 events, 16 speech segments, 91 OCR observations and 1 summary. All
stages were complete and the source was available for playback. The three
original videos and 880 observations were preserved without reindexing. Report:
`.cache/native-validation/real-clip-details.json`.

The first Finder launch of the locally signed .app requests system access to
Documents. When the app is signed again after a rebuild, macOS checks the new code
hash and may request permission again. A pending TCC prompt delays reading Python's
`pyvenv.cfg`; the app shows its loading screen until access is granted.

## Custom player and Play selection — 2026-11-25

AVKit displays only the video image; controls, volume, timeline and the pink
observation highlight are implemented in SwiftUI. OCR uses a frame marker rather
than an artificial long interval. A separate Play selection action seeks exactly
to the selected observation's start and starts playback only when seeking has
finished. Stale completion callbacks from cancelled seeks cannot start an old
selection. The selection sets neither a playback boundary nor a loop. ClipPlayback
owns playback decisions; recreating the NSView does not silently stop the source.

**7 real AVPlayer checks passed**: Play before the item is ready; seeking beyond
the selection and ordinary Play from the new position; restarting the selection
from its beginning; continuing beyond the selection's end; rapidly changing the
selection without late autoplay; an OCR frame without a stop after 1 ms; Pause
during seek and releasing the source on close. All runs were muted. Reproducible
command: `.venv/bin/python scripts/check_playback.py`; it generates a 12-second
test video and compiles `desktop/Tests/PlaybackChecks.swift`.

The native window was checked with the real `красивый ролик сна.mov`: the
00:16–00:26 window was selected and playback continued beyond it (01:05 was
observed). After seeking to 00:37, Play selection returned the source to 00:16
and started it. Pause and position changes through the accessible timeline were
also checked. The updated .app was built and its signature verified. Screenshot
of the resulting interface: `.cache/native-validation/custom-player.png`.
The index and source files were unchanged.

## Background mode and menu bar — 2026-11-25

Added an NSStatusItem VL, Hide to Menu Bar (⇧⌘M), window restoration, indexing
status and Pause/Resume in the menu. Hiding switches to the accessory activation
policy: windows and the Dock icon are hidden while the status icon remains.
Closing the last window no longer quits the app. Explicit Quit ends the UI and
its local service. Video previews pause when hidden; the separate indexing
worker is not told to stop.

The macOS 14+/Apple Silicon build and signature were verified. Native check:
Hide to Menu Bar → reopen .app → close the last window → reopen → explicit ⌘Q.
After hiding and closing the window, the app and service retained their PIDs;
the localhost API continued to return 3 videos and 880 observations. After Quit,
both processes ended, and the app was opened again. Restoration was checked by
reopening the .app, which calls the same showLibrary action as the status-menu
item. Report: `.cache/native-validation/menu-bar-check.json`.

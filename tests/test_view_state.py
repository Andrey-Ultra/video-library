from video_agent.app import snapshot
from video_agent.app_state import AppState
from video_agent.pipeline import item
from video_agent.store import STAGES, Store


def test_desktop_snapshot_uses_bounded_queries_and_preserves_counts(tmp_path, monkeypatch):
    folder = tmp_path / "library"
    folder.mkdir()
    with Store(folder) as store:
        for number in range(40):
            video = store.register(folder / f"{number}.mp4", f"fp-{number}", {"duration_ms": 10000})
            for stage in STAGES:
                store.prepare(video, stage, "sig")
                store.stage_status(video, stage, "complete")
            with store.db:
                store.put_item(item(video, "hierarchy", "summary", "root", 0, 10000, f"Summary {number}"))
                store.put_item(item(video, "speech", "speech", "0", 0, 1000, "hello"))
    state = AppState(tmp_path / "state")
    selected = state.select(str(folder))
    statements = []
    original = Store.__init__

    def traced(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.db.set_trace_callback(statements.append)

    monkeypatch.setattr(Store, "__init__", traced)
    view = snapshot(state)
    reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(reads) <= 10  # independent of number of videos
    assert len(view["videos"]) == 40 and view["item_count"] == 80
    assert view["active"]["id"] == selected["id"]
    assert all(v["item_count"] == 2 and v["vector_count"] == 0 for v in view["videos"])
    assert {v["summary"] for v in view["videos"]} == {f"Summary {i}" for i in range(40)}
    assert all(v["locations"] == [v["path"]] for v in view["videos"])

from hlsd.config import DaemonConfig
from hlsd.store import Store


def make_config(tmp_path):
    cfg = DaemonConfig(data_dir=tmp_path / ".hlsd")
    return cfg


def test_request_persistence_roundtrip(tmp_path):
    store = Store(make_config(tmp_path).db_path)
    spec = {"url": "https://x/live.m3u8?t=1", "mode": "window", "duration": 60}
    store.save_request("r1", spec, "scheduled")
    store.update_request("r1", state="active", activated_at=123.0)
    row = store.get_request("r1")
    assert row is not None
    assert row["state"] == "active"
    assert row["activated_at"] == 123.0
    assert row["spec"]


def test_job_persistence(tmp_path):
    store = Store(make_config(tmp_path).db_path)
    store.save_job("r1", 1000.0)
    store.save_job("r2", 500.0)
    assert store.list_jobs() == [("r2", 500.0), ("r1", 1000.0)]
    store.delete_job("r2")
    assert store.list_jobs() == [("r1", 1000.0)]


def test_segment_index_and_dedup_across_restarts(tmp_path):
    store = Store(make_config(tmp_path).db_path)
    for seq in range(5):
        store.save_segment("src1", "v", seq, f"https://x/seg{seq}.m4s", 4.0, f"/tmp/seg{seq}", 10, 0.0)
    assert store.get_segment_seqs("src1", "v") == {0, 1, 2, 3, 4}
    assert len(store.get_segments("src1", "v", since_seq=2)) == 2
    assert store.get_segment_seqs("src1", "a") == set()
    store.delete_source_segments("src1")
    assert store.get_segment_seqs("src1", "v") == set()

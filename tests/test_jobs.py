"""Tests for the generic disk-backed JobStore in _core/jobs.py.

These run without codex/git: the spawned command is a tiny python snippet whose
cwd is its own job dir, so writing ``result.json`` there mirrors what the real
worker does.
"""

from __future__ import annotations

import sys
import time

import pytest

from codex_in_claude._core.jobs import JobStore

# A snippet (run with cwd=job_dir) that writes the final envelope to result.json.
_WRITE_DONE = "import json; open('result.json','w').write(json.dumps({'ok': True, 'tool': 't'}))"


def _store(tmp_path, **kw) -> JobStore:
    opts = {"ttl_seconds": 3600, "max_seconds": 60, "max_count": 50}
    opts.update(kw)
    return JobStore(root=tmp_path / "jobs", **opts)


def _wait_terminal(store: JobStore, cwd: str, job_id: str, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = store.status(cwd, job_id)
        assert st is not None
        if st["status"] != "running":
            return st["status"]
        time.sleep(0.02)
    raise AssertionError("job did not terminate in time")


def _factory(code: str):
    return lambda _jd: [sys.executable, "-c", code]


def test_start_status_result_done(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    job_id, started = store.start(_factory(_WRITE_DONE), cwd, kind="codex_delegate")
    assert job_id and started
    assert _wait_terminal(store, cwd, job_id) == "done"
    rec, payload = store.result_payload(cwd, job_id, consume=False)
    assert rec["status"] == "done"
    assert payload == {"ok": True, "tool": "t"}
    assert rec["result_available"] is True


def test_failed_when_no_result(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    job_id, _ = store.start(_factory("raise SystemExit(1)"), cwd, kind="k")
    assert _wait_terminal(store, cwd, job_id) == "failed"
    rec, payload = store.result_payload(cwd, job_id, consume=False)
    assert rec["status"] == "failed"
    assert payload is None


def test_cancel_running(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    job_id, _ = store.start(_factory("import time; time.sleep(30)"), cwd, kind="k")
    st = store.cancel(cwd, job_id)
    assert st["status"] == "cancelled"
    # cancelling again returns the terminal record unchanged
    assert store.cancel(cwd, job_id)["status"] == "cancelled"


def test_consume_deletes(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    job_id, _ = store.start(_factory(_WRITE_DONE), cwd, kind="k")
    assert _wait_terminal(store, cwd, job_id) == "done"
    _, payload = store.result_payload(cwd, job_id, consume=True)
    assert payload == {"ok": True, "tool": "t"}
    assert store.status(cwd, job_id) is None


def test_consume_nondone_keeps_record(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    job_id, _ = store.start(_factory("import time; time.sleep(30)"), cwd, kind="k")
    store.cancel(cwd, job_id)
    rec, payload = store.result_payload(cwd, job_id, consume=True)
    assert rec["status"] == "cancelled"
    assert payload is None
    # not deleted (non-done)
    assert store.status(cwd, job_id) is not None


def test_missing_job(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    assert store.status(cwd, "deadbeef") is None
    assert store.cancel(cwd, "deadbeef") is None
    rec, payload = store.result_payload(cwd, "deadbeef", consume=False)
    assert rec is None and payload is None


def test_deadline_timeout(tmp_path):
    store = _store(tmp_path, max_seconds=1)
    cwd = str(tmp_path)
    job_id, _ = store.start(_factory("import time; time.sleep(30)"), cwd, kind="k")
    time.sleep(1.2)
    assert store.status(cwd, job_id)["status"] == "timeout"


def test_extra_roundtrips(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    job_id, _ = store.start(_factory(_WRITE_DONE), cwd, kind="k", extra={"foo": "bar"})
    _wait_terminal(store, cwd, job_id)
    assert store.status(cwd, job_id)["extra"] == {"foo": "bar"}


def test_write_spec_lands_in_job_dir(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    seen = {}

    def factory(jd):
        seen["jd"] = jd
        return [sys.executable, "-c", _WRITE_DONE]

    job_id, _ = store.start(factory, cwd, kind="k", write_spec={"task": "x"})
    _wait_terminal(store, cwd, job_id)
    import json

    assert json.loads((seen["jd"] / "spec.json").read_text()) == {"task": "x"}


def test_list_newest_first_and_count_cap(tmp_path):
    store = _store(tmp_path, max_count=2)
    cwd = str(tmp_path)
    ids = []
    for _ in range(3):
        jid, _ = store.start(_factory(_WRITE_DONE), cwd, kind="k")
        _wait_terminal(store, cwd, jid)
        ids.append(jid)
        time.sleep(0.01)
    listed = store.list_jobs(cwd)
    assert len(listed) <= 2  # oldest terminal evicted at the cap
    # newest first
    epochs = [j["started_epoch"] for j in listed]
    assert epochs == sorted(epochs, reverse=True)


def test_ttl_eviction(tmp_path):
    store = _store(tmp_path, ttl_seconds=60)
    cwd = str(tmp_path)
    job_id, _ = store.start(_factory(_WRITE_DONE), cwd, kind="k")
    _wait_terminal(store, cwd, job_id)
    # Force the completion far into the past, then a list() reap should drop it.
    store.list_jobs(cwd)
    jd = store._job_dir(cwd, job_id)
    import json

    meta = json.loads((jd / "meta.json").read_text())
    meta["completed_epoch"] = time.time() - 10_000
    (jd / "meta.json").write_text(json.dumps(meta))
    store.list_jobs(cwd)
    assert store.status(cwd, job_id) is None


def test_list_empty_workspace(tmp_path):
    store = _store(tmp_path)
    assert store.list_jobs(str(tmp_path)) == []


def test_start_oserror_cleans_up(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    with pytest.raises(OSError):
        # a non-existent executable path makes Popen raise OSError
        store.start(lambda _jd: ["/nonexistent/bin/zzz-not-real"], cwd, kind="k")


# --- defensive helpers / edge branches ---------------------------------------
import os  # noqa: E402

from codex_in_claude._core import jobs  # noqa: E402


def test_pid_alive_none_and_dead():
    assert jobs._pid_alive(None) is False
    # An almost-certainly-unused PID raises ProcessLookupError -> False.
    assert jobs._pid_alive(2**30) is False


def test_pid_alive_self():
    # Our own PID is alive (and not reapable via kill(0)).
    assert jobs._pid_alive(os.getpid()) is True


def test_is_running_none_and_not_our_child():
    assert jobs._is_running(None) is False
    # os.getpid() is alive but not our child: waitpid raises ChildProcessError,
    # then the kill(0) liveness probe reports it alive.
    assert jobs._is_running(os.getpid()) is True
    # A dead PID: waitpid raises ChildProcessError, liveness probe returns False.
    assert jobs._is_running(2**30) is False


def test_kill_pid_tree_none_is_noop():
    jobs._kill_pid_tree(None)  # must not raise


def test_read_meta_and_envelope_missing(tmp_path):
    assert JobStore._read_meta(tmp_path / "nope") is None
    assert JobStore._read_envelope(tmp_path / "nope") is None


def test_read_envelope_garbage_and_nonobject(tmp_path):
    (tmp_path / "result.json").write_text("not json {")
    assert JobStore._read_envelope(tmp_path) is None
    (tmp_path / "result.json").write_text("[1, 2, 3]")  # valid JSON, not an object
    assert JobStore._read_envelope(tmp_path) is None
    (tmp_path / "result.json").write_text("   ")  # blank
    assert JobStore._read_envelope(tmp_path) is None


def test_rmtree_missing_is_silent(tmp_path):
    JobStore._rmtree(tmp_path / "does-not-exist")  # must not raise


def test_reap_skips_non_dir_entries(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    # create the workspace dir, then drop a stray file beside job dirs
    job_id, _ = store.start(_factory(_WRITE_DONE), cwd, kind="k")
    _wait_terminal(store, cwd, job_id)
    ws = store._ws_dir(cwd)
    (ws / "stray.txt").write_text("x")
    # list_jobs reaps the workspace and must ignore the stray file
    listed = store.list_jobs(cwd)
    assert any(j["job_id"] == job_id for j in listed)


def test_read_meta_unparseable(tmp_path):
    jd = tmp_path / "jd"
    jd.mkdir()
    (jd / "meta.json").write_text("{bad json")
    assert JobStore._read_meta(jd) is None


def test_count_cap_keeps_running_job(tmp_path):
    # max_count=1 with one running + one done: the running job is never evicted.
    store = _store(tmp_path, max_count=1)
    cwd = str(tmp_path)
    running_id, _ = store.start(_factory("import time; time.sleep(30)"), cwd, kind="k")
    done_id, _ = store.start(_factory(_WRITE_DONE), cwd, kind="k")
    _wait_terminal(store, cwd, done_id)
    # starting a third (done) job triggers the cap; the running job stays.
    third_id, _ = store.start(_factory(_WRITE_DONE), cwd, kind="k")
    _wait_terminal(store, cwd, third_id)
    assert store.status(cwd, running_id) is not None
    store.cancel(cwd, running_id)


def test_deadline_and_expiry_helpers(tmp_path):
    store = _store(tmp_path)
    # missing started/deadline -> falls back to max_seconds
    assert store._deadline_seconds({}) == store.max_seconds
    # missing completed_epoch -> no expiry, not expired
    assert store._expires_at({}) is None
    assert store._expired({}) is False


def test_read_envelope_oserror(tmp_path):
    # result.json is a directory -> reading raises OSError, handled as None
    (tmp_path / "result.json").mkdir()
    assert JobStore._read_envelope(tmp_path) is None


def test_reap_and_list_skip_unparseable_meta(tmp_path):
    store = _store(tmp_path)
    cwd = str(tmp_path)
    # a healthy done job, plus a job dir with unparseable meta
    good_id, _ = store.start(_factory(_WRITE_DONE), cwd, kind="k")
    _wait_terminal(store, cwd, good_id)
    bad = store._ws_dir(cwd) / "deadbeef"
    bad.mkdir()
    (bad / "meta.json").write_text("{not json")
    listed = store.list_jobs(cwd)  # exercises reap + list skip-None-meta branches
    ids = {j["job_id"] for j in listed}
    assert good_id in ids
    assert "deadbeef" not in ids

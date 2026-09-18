"""Flag-sync behavior of the vendored mirror (bin/subfleet-mirror).

The full behavior suite lives in the public cc-mirror-sessions export; this
file covers what the canonical copy adds ahead of that (Max-gated) sync —
currently the isStarred propagation (2026-08-24), tested with the same
synthetic-store fixtures the public suite uses.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "bin" / "subfleet-mirror"


@pytest.fixture
def m():
    loader = importlib.machinery.SourceFileLoader("subfleet_mirror", str(SCRIPT))
    spec = importlib.util.spec_from_loader("subfleet_mirror", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def make_store(m, tmp_path, monkeypatch):
    base = tmp_path / "store"
    proj = tmp_path / "projects" / "-work-repo"
    proj.mkdir(parents=True)
    monkeypatch.setattr(m, "BASE", str(base))
    monkeypatch.setattr(m, "PROJ", str(tmp_path / "projects"))
    monkeypatch.setattr(m, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(m, "LOCKFILE", str(tmp_path / "lock"))
    monkeypatch.setattr(m, "CONFIG", str(tmp_path / "no-config.json"))
    return base, proj


def write_entry(base, acct, org, sid, cli, title, *, archived=False, starred=False, activity=100):
    d = base / acct / org
    d.mkdir(parents=True, exist_ok=True)
    fp = d / f"local_{sid}.json"
    fp.write_text(json.dumps({
        "sessionId": f"local_{sid}",
        "cliSessionId": cli,
        "cwd": "/work/repo",
        "createdAt": 1,
        "lastActivityAt": activity,
        "isArchived": archived,
        "isStarred": starred,
        "title": title,
        "titleSource": "auto",
    }))
    return fp


def read_entry(base, acct, org, sid):
    return json.loads((base / acct / org / f"local_{sid}.json").read_text())


def run_main(m, monkeypatch, argv=("--quiet",)):
    monkeypatch.setattr(sys, "argv", ["subfleet-mirror", *argv])
    assert m.main() == 0


def test_star_flip_propagates_both_directions(m, tmp_path, monkeypatch):
    base, proj = make_store(m, tmp_path, monkeypatch)
    (proj / "cli-1.jsonl").write_text('{"type":"custom-title","customTitle":"T"}\n')
    write_entry(base, "acctA", "orgA", "s1", "cli-1", "T")
    write_entry(base, "acctB", "orgB", "s1", "cli-1", "T")
    run_main(m, monkeypatch)  # record base (unstarred)

    write_entry(base, "acctA", "orgA", "s1", "cli-1", "T", starred=True)
    run_main(m, monkeypatch)
    assert read_entry(base, "acctB", "orgB", "s1")["isStarred"] is True
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["cli-1"]["isStarred"] is True

    # un-star in the OTHER account: the change wins again
    write_entry(base, "acctB", "orgB", "s1", "cli-1", "T", starred=False)
    run_main(m, monkeypatch)
    assert read_entry(base, "acctA", "orgA", "s1")["isStarred"] is False


def test_star_bootstrap_without_base_prefers_starred(m, tmp_path, monkeypatch):
    base, proj = make_store(m, tmp_path, monkeypatch)
    (proj / "cli-1.jsonl").write_text('{"type":"custom-title","customTitle":"T"}\n')
    write_entry(base, "acctA", "orgA", "s1", "cli-1", "T", starred=True)
    write_entry(base, "acctB", "orgB", "s1", "cli-1", "T", starred=False)
    run_main(m, monkeypatch)  # first-ever sync: no merge base
    assert read_entry(base, "acctB", "orgB", "s1")["isStarred"] is True


def test_star_sync_respects_no_flag_sync_and_archive_still_works(m, tmp_path, monkeypatch):
    base, proj = make_store(m, tmp_path, monkeypatch)
    (proj / "cli-1.jsonl").write_text('{"type":"custom-title","customTitle":"T"}\n')
    write_entry(base, "acctA", "orgA", "s1", "cli-1", "T", starred=True)
    write_entry(base, "acctB", "orgB", "s1", "cli-1", "T", starred=False)
    run_main(m, monkeypatch, argv=("--quiet", "--no-flag-sync"))
    assert read_entry(base, "acctB", "orgB", "s1")["isStarred"] is False

    write_entry(base, "acctA", "orgA", "s2", "cli-2", "U", archived=True)
    write_entry(base, "acctB", "orgB", "s2", "cli-2", "U", archived=False)
    (proj / "cli-2.jsonl").write_text('{"type":"custom-title","customTitle":"U"}\n')
    run_main(m, monkeypatch)
    assert read_entry(base, "acctB", "orgB", "s2")["isArchived"] is True


def test_ultracode_default_fills_missing_but_respects_explicit_false(m, tmp_path, monkeypatch):
    """2026-08-26: spawned sessions arrive with no sessionSettings, defeating
    the ultracode-global ruling. The mirror fills the missing key; an explicit
    false (the user turned it off) is never overridden."""
    base, proj = make_store(m, tmp_path, monkeypatch)
    for i, cli in enumerate(["cli-1", "cli-2", "cli-3"], start=1):
        (proj / f"{cli}.jsonl").write_text('{"type":"custom-title","customTitle":"T"}\n')
        write_entry(base, "acctA", "orgA", f"s{i}", cli, "T")
    d = base / "acctA" / "orgA"
    # s1: no sessionSettings at all (the spawn-path shape)
    # s2: settings dict without the key
    e2 = read_entry(base, "acctA", "orgA", "s2"); e2["sessionSettings"] = {"other": 1}
    (d / "local_s2.json").write_text(json.dumps(e2))
    # s3: explicit false — the user's own act
    e3 = read_entry(base, "acctA", "orgA", "s3"); e3["sessionSettings"] = {"ultracode": False}
    (d / "local_s3.json").write_text(json.dumps(e3))

    run_main(m, monkeypatch)
    assert read_entry(base, "acctA", "orgA", "s1")["sessionSettings"] == {"ultracode": True}
    assert read_entry(base, "acctA", "orgA", "s2")["sessionSettings"] == {"other": 1, "ultracode": True}
    assert read_entry(base, "acctA", "orgA", "s3")["sessionSettings"] == {"ultracode": False}

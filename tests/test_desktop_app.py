"""The desktop app's staged update, read from its main.log, surfaced as one
line in `subfleet status` (subfleet/desktop_app.py)."""

from __future__ import annotations

import json
import plistlib
from datetime import datetime, timedelta
from pathlib import Path

from subfleet import cli, desktop_app, render
from subfleet.util import now_local
from test_pick import entry
from test_watchdog import snap

V = "1.46388.4"


def _line(stamp: datetime, body: str) -> str:
    return f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} [info] {body}"


def _log(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "main.log"
    path.write_text("\n".join(lines) + "\n")
    return path


def _staging(now: datetime, *, since_hours: float = 5.5, last_seen_min: float = 20.0) -> list[str]:
    start = now - timedelta(hours=since_hours)
    lines = [_line(start - timedelta(seconds=10), "[updater] Found an update, downloading"),
             _line(start, f"[updater] Update downloaded and ready to install {{ releaseName: 'Claude {V}' }}")]
    stamp = start + timedelta(minutes=20)
    end = now - timedelta(minutes=last_seen_min)
    while stamp <= end:
        lines.append(_line(stamp, f"[updater] Staged version {V} is still current (latest: {V}, lastTarget: null)"))
        lines.append(_line(stamp + timedelta(seconds=30), "[process-memory] sys_free=57GB"))
        stamp += timedelta(minutes=20)
    return lines


def test_staged_update_is_reported_with_its_first_announcement(tmp_path):
    now = now_local().replace(microsecond=0)
    path = _log(tmp_path, _staging(now))
    staged = desktop_app.staged_update(path, now=now, installed="1.44121.4")
    assert staged["version"] == V and staged["stale"] is False and staged["since_at_least"] is False
    assert datetime.fromisoformat(staged["since"]) == now - timedelta(hours=5.5)
    # heartbeats every 20 min, generated up to last_seen_min before now
    assert timedelta(minutes=20) <= (now - datetime.fromisoformat(staged["last_seen"])) <= timedelta(minutes=40)
    assert staged["quit_for_update_at"] is None and staged["log"] == str(path)
    line = desktop_app.warning_line({"staged_update": staged}, now)
    assert line.startswith("  WARNING: desktop app update staged since ")
    assert f"({V}); it will kill every app-hosted session when applied" in line


def test_applied_replaced_installed_and_stale_stagings_are_not_warned(tmp_path):
    now = now_local().replace(microsecond=0)
    base = _staging(now)
    # applied: the update quit, then the new version launched
    applied = base + [_line(now - timedelta(minutes=5), "beforeQuitForUpdate handler fired, going down for update"),
                      _line(now - timedelta(minutes=4), f"[updater] Version changed since last launch: 1.44121.4 → {V}"),
                      _line(now - timedelta(minutes=4), f"[updater] Previous update install succeeded (1.44121.4 -> {V})")]
    assert desktop_app.staged_update(_log(tmp_path, applied), now=now, installed="1.44121.4") is None
    # quit announced, app not back yet: still staged, and the quit is named
    quit_only = base + [_line(now - timedelta(minutes=5), "beforeQuitForUpdate handler fired, going down for update")]
    staged = desktop_app.staged_update(_log(tmp_path, quit_only), now=now, installed="1.44121.4")
    assert staged["quit_for_update_at"] and "already quit for the update at" in desktop_app.warning_line({"staged_update": staged}, now)
    # the installed app already IS the staged version: nothing pending
    assert desktop_app.staged_update(_log(tmp_path, base), now=now, installed=V) is None
    # replaced by a newer staging: the newer version, since the replacement
    replaced = base + [_line(now - timedelta(minutes=15), f"[updater] [evt:replacing-staged] Newer version 1.47000.1 available (staged: {V}), replacing staged update"),
                       _line(now - timedelta(minutes=14), "[updater] Update downloaded and ready to install { releaseName: 'Claude 1.47000.1' }"),
                       _line(now - timedelta(minutes=1), "[updater] Staged version 1.47000.1 is still current (latest: 1.47000.1, lastTarget: null)")]
    staged = desktop_app.staged_update(_log(tmp_path, replaced), now=now, installed=V)
    assert staged["version"] == "1.47000.1" and datetime.fromisoformat(staged["since"]) == now - timedelta(minutes=14)
    # not announced for over 90 minutes: stale, and the status line stays quiet
    stale = desktop_app.staged_update(_log(tmp_path, _staging(now, last_seen_min=120)), now=now, installed="1.44121.4")
    assert stale["stale"] is True and desktop_app.warning_line({"staged_update": stale}, now) is None
    assert desktop_app.warning_line(None) is None and desktop_app.warning_line({"staged_update": None}) is None
    # no log, empty log
    assert desktop_app.staged_update(tmp_path / "missing.log", now=now) is None
    assert desktop_app.staged_update(_log(tmp_path, [""]), now=now) is None


def test_a_staging_that_began_before_the_scanned_tail_is_at_least_since(tmp_path, monkeypatch):
    now = now_local().replace(microsecond=0)
    heartbeats_only = [line for line in _staging(now) if "still current" in line or "process-memory" in line]
    path = _log(tmp_path, heartbeats_only)
    staged = desktop_app.staged_update(path, now=now, installed="1.44121.4")
    assert staged["since_at_least"] is True
    assert datetime.fromisoformat(staged["since"]) == now - timedelta(hours=5.5) + timedelta(minutes=20)
    assert "staged since at least " in desktop_app.warning_line({"staged_update": staged}, now)
    monkeypatch.setattr(desktop_app, "_TAIL_BYTES", 400)
    truncated = desktop_app.staged_update(path, now=now, installed="1.44121.4")
    assert truncated["version"] == V and truncated["since_at_least"] is True


def test_installed_version_and_status_read_the_plist_and_the_log(tmp_path, monkeypatch):
    plist = tmp_path / "Info.plist"
    with plist.open("wb") as stream:
        plistlib.dump({"CFBundleVersion": V}, stream)
    assert desktop_app.installed_version(plist) == V
    assert desktop_app.installed_version(tmp_path / "nope.plist") is None
    monkeypatch.setenv("SUBFLEET_DESKTOP_PLIST", str(plist))
    now = now_local().replace(microsecond=0)
    log = _log(tmp_path, _staging(now))
    monkeypatch.setenv("SUBFLEET_DESKTOP_MAIN_LOG", str(log))
    section = desktop_app.status(now=now)
    assert section["installed_version"] == V and section["main_log"] == str(log)
    assert section["staged_update"] is None, "the staged version is already installed"
    section = desktop_app.status(now=now, installed="1.44121.4")
    assert section["staged_update"]["version"] == V


def test_status_table_carries_the_warning_line_even_from_a_cached_snapshot(env_paths, tmp_path, monkeypatch, capsys):
    now = now_local().replace(microsecond=0)
    log = _log(tmp_path, _staging(now))
    monkeypatch.setenv("SUBFLEET_DESKTOP_MAIN_LOG", str(log))
    with (tmp_path / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleVersion": "1.44121.4"}, stream)
    monkeypatch.setenv("SUBFLEET_DESKTOP_PLIST", str(tmp_path / "Info.plist"))
    cached = snap([entry("/h/.codex-3", 5, account="b")])
    env_paths["state"].mkdir(parents=True, exist_ok=True)
    (env_paths["state"] / "snapshot.json").write_text(json.dumps(cached))
    assert cli.main(["status", "--cached"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[2].startswith("  WARNING: desktop app update staged since ")
    assert "it will kill every app-hosted session when applied" in lines[2]
    assert cli.main(["status", "--cached", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["desktop_app"]["staged_update"]["version"] == V
    # nothing staged → no warning line at all
    monkeypatch.setenv("SUBFLEET_DESKTOP_MAIN_LOG", str(tmp_path / "absent.log"))
    assert cli.main(["status", "--cached"]) == 0
    assert "WARNING: desktop app update" not in capsys.readouterr().out
    assert "WARNING" not in render.table(cached)

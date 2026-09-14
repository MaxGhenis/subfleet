"""A Codex weekly window starts on the first real request; idle lanes after a
global reset must be touched immediately (Max 2026-09-14)."""
import argparse
import json
from datetime import timedelta

from subfleet import cli, codex, render, snapshot, watchdog
from subfleet.util import load_json, now_local, parse_iso

from test_pick import entry
from test_watchdog import read_alerts, snap

WEEK = 604800


def unstarted_probe(now=None, offset_s=0.0, used=0, limit_reached=False, seconds=WEEK):
    """wham as it reads a window that has not started: reset_at = checked_at + length."""
    now = now or now_local()
    return {
        "status": "ok", "checked_at": now.isoformat(timespec="seconds"),
        "email": "a@x.com", "plan_type": "pro",
        "allowed": not limit_reached, "limit_reached": limit_reached,
        "primary": {"used_percent": used, "window_seconds": seconds,
                    "reset_at": (now + timedelta(seconds=seconds + offset_s)).isoformat(timespec="seconds")},
        "secondary": None,
        "additional": [],
    }


def started_probe(now=None, used=9):
    now = now or now_local()
    return {
        "status": "ok", "checked_at": now.isoformat(timespec="seconds"),
        "email": "a@x.com", "plan_type": "pro", "allowed": True, "limit_reached": False,
        "primary": {"used_percent": used, "window_seconds": WEEK,
                    "reset_at": (now + timedelta(days=6, hours=23)).isoformat(timespec="seconds")},
        "secondary": None, "additional": [],
    }


def lane(home, probe, account="a", verdict="ok"):
    e = entry(home, 0, weekly=0, verdict=verdict, account=account)
    e["probe"] = probe
    e["windows"] = snapshot.effective_windows(probe, None)
    e["window_unstarted"] = codex.window_unstarted(probe)
    return e


class TestDetection:
    def test_sliding_reset_at_zero_percent_is_unstarted(self):
        assert codex.window_unstarted(unstarted_probe()) is True
        assert codex.window_unstarted(unstarted_probe(offset_s=-200)) is True

    def test_started_windows_are_not(self):
        assert codex.window_unstarted(started_probe()) is False
        assert codex.window_unstarted(unstarted_probe(used=1)) is False
        assert codex.window_unstarted(unstarted_probe(offset_s=-2 * 3600)) is False

    def test_only_weekly_windows_count(self):
        assert codex.window_unstarted(unstarted_probe(seconds=18000)) is False

    def test_limited_or_bad_probes_are_not(self):
        assert codex.window_unstarted(unstarted_probe(limit_reached=True)) is False
        assert codex.window_unstarted({"status": "http-401"}) is False
        assert codex.window_unstarted(None) is False

    def test_snapshot_flags_and_fleet_lists_unstarted_homes(self):
        homes = [lane("/h/.codex-1", unstarted_probe()), lane("/h/.codex-2", started_probe(), account="b")]
        fleet = snapshot.codex_fleet(homes, now_local())
        assert fleet["unstarted"] == ["/h/.codex-1"]


class TestWatchdogTouch:
    def _fakes(self, monkeypatch, touch_result=None, reprobe_used=1):
        calls = {"touch": [], "reprobe": []}

        def fake_touch(home, **kwargs):
            calls["touch"].append(str(home))
            return dict(touch_result or {"status": "ok", "rc": 0, "detail": ""})

        def fake_reprobe(home):
            calls["reprobe"].append(str(home))
            return {"auth": {"status": "ok", "email": "a@x.com", "plan": "pro",
                             "account_id": "a", "last_refresh": "2026-09-14T10:00:00Z"},
                    "probe": started_probe(used=reprobe_used), "observed": None}

        monkeypatch.setattr(watchdog.codex, "touch_window", fake_touch)
        monkeypatch.setattr(watchdog, "_reprobe_home", fake_reprobe)
        return calls

    def test_touches_only_unstarted_lanes_and_records_state(self, env_paths, monkeypatch):
        calls = self._fakes(monkeypatch)
        s = snap([lane("/h/.codex-1", unstarted_probe()),
                  lane("/h/.codex-2", started_probe(), account="b"),
                  lane("/h/.codex-3", unstarted_probe(limit_reached=True), account="c",
                       verdict="limited")])
        events = watchdog.touch_unstarted_codex_windows(s)
        assert calls["touch"] == ["/h/.codex-1"]
        assert calls["reprobe"] == ["/h/.codex-1"]
        assert [ev["result"] for ev in events] == ["touched"]
        touched = s["codex"]["homes"][0]
        assert touched["window_unstarted"] is False
        assert touched["windows"]["weekly"]["used_percent"] == 1
        assert touched["window_touch"]["model"] == "gpt-5.6-luna"
        from subfleet import paths

        state = load_json(paths.window_touch_path())
        assert state["/h/.codex-1"]["result"] == "touched"
        assert parse_iso(state["/h/.codex-1"]["weekly_reset_at"]) is not None
        assert s["codex"]["fleet"]["unstarted"] == []

    def test_dry_run_touches_nothing(self, env_paths, monkeypatch):
        calls = self._fakes(monkeypatch)
        s = snap([lane("/h/.codex-1", unstarted_probe())])
        events = watchdog.touch_unstarted_codex_windows(s, dry_run=True)
        assert calls["touch"] == []
        assert [ev["result"] for ev in events] == ["dry-run"]

    def test_recent_attempt_is_not_repeated_until_spacing_elapses(self, env_paths, monkeypatch):
        calls = self._fakes(monkeypatch)
        from subfleet import paths
        from subfleet.util import atomic_write_json, iso

        atomic_write_json(paths.window_touch_path(), {
            "/h/.codex-1": {"attempted_at": iso(now_local() - timedelta(minutes=10)),
                            "result": "touched"}
        })
        s = snap([lane("/h/.codex-1", unstarted_probe())])
        assert watchdog.touch_unstarted_codex_windows(s) == []
        assert calls["touch"] == []
        # The explicit lane form ignores spacing and the unstarted check.
        s2 = snap([lane("/h/.codex-1", started_probe())])
        events = watchdog.touch_unstarted_codex_windows(s2, only={"/h/.codex-1"}, force=True)
        assert calls["touch"] == ["/h/.codex-1"]
        assert events[0]["result"] == "touched"

    def test_revoked_touch_downgrades_the_lane(self, env_paths, monkeypatch):
        calls = self._fakes(monkeypatch, {"status": "revoked", "rc": 1,
                                          "detail": "refresh token was revoked"})
        s = snap([lane("/h/.codex-1", unstarted_probe())])
        events = watchdog.touch_unstarted_codex_windows(s)
        assert calls["reprobe"] == []
        assert events[0]["result"] == "revoked"
        assert s["codex"]["homes"][0]["verdict"] == "auth-revoked"

    def test_watchdog_cycle_touches_and_notifies(self, env_paths, monkeypatch):
        calls = self._fakes(monkeypatch)
        s = snap([lane("/h/.codex-1", unstarted_probe()),
                  lane("/h/.codex-2", started_probe(), account="b")])
        summary = watchdog.run(snap=s)
        assert calls["touch"] == ["/h/.codex-1"]
        assert summary["touched"] == ["/h/.codex-1"]
        out = read_alerts(env_paths)
        assert "weekly clock started on 1 lane" in out
        assert "~/h/.codex-1" in out or "/h/.codex-1" in out


class TestTouchCli:
    def _args(self, **kw):
        base = dict(target=None, dry_run=False, json=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_touch_all_reports_events_and_persists_snapshot(self, env_paths, monkeypatch, capsys):
        s = snap([lane("/h/.codex-1", unstarted_probe()),
                  lane("/h/.codex-2", started_probe(), account="b")])
        monkeypatch.setattr(cli.snapshot, "build", lambda **kw: s)
        seen = {}

        def fake_touch(snap_arg, dry_run=False, only=None, force=False, **kw):
            seen.update(only=only, force=force, dry_run=dry_run)
            return [{"home": "/h/.codex-1", "result": "touched", "detail": "",
                     "weekly_reset_at": "2026-09-21T13:43:41-04:00"}]

        monkeypatch.setattr(cli.watchdog, "touch_unstarted_codex_windows", fake_touch)
        assert cli.cmd_touch_codex(self._args()) == 0
        out = capsys.readouterr().out
        assert "/h/.codex-1: touched" in out and "weekly reset now 2026-09-21" in out
        assert seen == {"only": None, "force": False, "dry_run": False}
        from subfleet import paths

        assert load_json(paths.snapshot_path())["codex"]["homes"][0]["home"] == "/h/.codex-1"

    def test_touch_named_lane_is_forced(self, env_paths, monkeypatch, capsys):
        s = snap([lane("/h/.codex-2", started_probe(), account="b")])
        monkeypatch.setattr(cli.snapshot, "build", lambda **kw: s)
        monkeypatch.setattr(cli, "_reset_target_homes", lambda target: ["/h/.codex-2"])
        seen = {}

        def fake_touch(snap_arg, dry_run=False, only=None, force=False, **kw):
            seen.update(only=only, force=force)
            return [{"home": "/h/.codex-2", "result": "touched", "detail": "",
                     "weekly_reset_at": None}]

        monkeypatch.setattr(cli.watchdog, "touch_unstarted_codex_windows", fake_touch)
        assert cli.cmd_touch_codex(self._args(target="2", json=True)) == 0
        data = json.loads(capsys.readouterr().out)
        assert seen == {"only": {"/h/.codex-2"}, "force": True}
        assert data["events"][0]["result"] == "touched"

    def test_nothing_to_touch_says_so(self, env_paths, monkeypatch, capsys):
        s = snap([lane("/h/.codex-2", started_probe(), account="b")])
        monkeypatch.setattr(cli.snapshot, "build", lambda **kw: s)
        monkeypatch.setattr(cli.watchdog, "touch_unstarted_codex_windows", lambda *a, **k: [])
        assert cli.cmd_touch_codex(self._args()) == 0
        assert "already running" in capsys.readouterr().out


def test_status_table_labels_unstarted_clocks(env_paths):
    s = snap([lane("/h/.codex-1", unstarted_probe()),
              lane("/h/.codex-2", started_probe(), account="b")])
    s["codex"]["fleet"]["unstarted"] = ["/h/.codex-1"]
    out = render.table(s)
    row = next(line for line in out.splitlines() if "a@x.com" in line)
    assert "clock not started (touch)" in row
    assert "clocks not started: 1" in out

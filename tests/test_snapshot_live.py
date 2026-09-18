"""Active-account live-source selection through snapshot.build + rendering.

The 2026-08-12 regression this guards: the statusline tap only fires in
terminal-TUI sessions (last real capture 2026-07-22), yet a 3-week-old capture
was headlined as the CLAUDE 5h number. The snapshot must lead with the
freshest available source and demote anything stale.
"""

import json
from datetime import timedelta

from subfleet import claude, render, snapshot
from subfleet.util import iso, now_local


def _ago(**kw):
    return (now_local() - timedelta(**kw)).isoformat(timespec="seconds")


def _setup(monkeypatch):
    monkeypatch.setattr(
        claude,
        "keychain_credentials",
        lambda: {"status": "ok", "subscription": "max", "tier": "t", "_token": "tok"},
    )
    monkeypatch.setattr(claude, "known_accounts", lambda: ["max@example.com"])
    monkeypatch.setattr(
        claude, "roster_config", lambda: {"accounts": ["max@example.com"], "enrolled": {}}
    )


def _failed_probe(token, timeout=15):
    return {"status": "token-invalid"}


def _write_statusline(state, pct=56.0, wk=51, age=timedelta(days=21)):
    state.mkdir(parents=True, exist_ok=True)
    (state / "claude-statusline.json").write_text(
        json.dumps(
            {
                "updated_at": (now_local() - age).isoformat(timespec="seconds"),
                "rate_limits": {
                    "five_hour": {"used_percentage": pct, "resets_at": 1784753400},
                    "seven_day": {"used_percentage": wk, "resets_at": 1785214800},
                },
            }
        )
    )


def _write_oauth_raw(state, pct=42.5, wk=61, age=timedelta(hours=2)):
    state.mkdir(parents=True, exist_ok=True)
    (state / "claude-oauth-raw.json").write_text(
        json.dumps(
            {
                "checked_at": (now_local() - age).isoformat(timespec="seconds"),
                "raw": {
                    "five_hour": {"used_percentage": pct, "resets_at": None},
                    "seven_day": {"used_percentage": wk, "resets_at": None},
                },
            }
        )
    )


def _write_capacity_cache(state, email, pct=33, wk=40, age=timedelta(minutes=30)):
    state.mkdir(parents=True, exist_ok=True)
    (state / "capacity-live-cache.json").write_text(
        json.dumps(
            {
                "probed_at": (now_local() - age).isoformat(timespec="seconds"),
                "accounts": [
                    {
                        "family": "claude",
                        "active": True,
                        "email": email,
                        "five_hour": {"used_percent": pct, "confidence": "live"},
                        "weekly": {"used_percent": wk, "confidence": "live"},
                    }
                ],
            }
        )
    )


class TestActiveLiveSelection:
    def test_oauth_cache_beats_ancient_statusline(self, env_paths, monkeypatch):
        _setup(monkeypatch)
        _write_statusline(env_paths["state"])  # 3 weeks old
        _write_oauth_raw(env_paths["state"])  # 2 hours old
        snap = snapshot.build(live=True, claude_probe_fn=_failed_probe)
        live = snap["claude"]["live"]
        assert live["source"] == "oauth-cache"
        assert live["five_hour_pct"] == 42.5
        assert live["stale"] is False
        assert snap["claude"]["verdict"] == "ok"
        out = render.table(snap)
        assert "5h window: 42.5% used (oauth-cache" in out
        assert "stale, from" not in out

    def test_stale_statusline_demoted_not_headlined(self, env_paths, monkeypatch):
        _setup(monkeypatch)
        _write_statusline(env_paths["state"])  # only source, 3 weeks old
        snap = snapshot.build(live=True, claude_probe_fn=_failed_probe)
        live = snap["claude"]["live"]
        assert live["source"] == "statusline"
        assert live["stale"] is True
        assert snap["claude"]["verdict"] == "unknown"
        out = render.table(snap)
        assert "5h window: 56.0% used" not in out
        assert "stale, from statusline" in out
        brief = render.brief_md(snap)
        assert "- claude: 5h 56" not in brief
        assert "stale, from statusline" in brief

    def test_live_probe_wins_and_rewrites_cache(self, env_paths, monkeypatch):
        _setup(monkeypatch)
        _write_statusline(env_paths["state"])
        _write_oauth_raw(env_paths["state"], pct=99, age=timedelta(hours=10))

        def ok_probe(token, timeout=15):
            stamp = iso(now_local())
            window = {"used_percent": 12.0, "reset_at": None}
            return {
                "status": "ok",
                "checked_at": stamp,
                "raw": {"five_hour": {"used_percentage": 12.0, "resets_at": None}},
                "five_hour": window,
                "windows": {"five_hour": window},
            }

        snap = snapshot.build(live=True, claude_probe_fn=ok_probe)
        live = snap["claude"]["live"]
        assert live["source"] == "oauth"
        assert live["five_hour_pct"] == 12.0
        assert live["stale"] is False
        raw = json.loads((env_paths["state"] / "claude-oauth-raw.json").read_text())
        assert raw["raw"]["five_hour"]["used_percentage"] == 12.0
        assert "5h window: 12.0% used (oauth " in render.table(snap)

    def test_capacity_cache_freshest_wins(self, env_paths, monkeypatch):
        _setup(monkeypatch)
        _write_statusline(env_paths["state"])
        _write_oauth_raw(env_paths["state"], age=timedelta(hours=10))
        _write_capacity_cache(env_paths["state"], "max@example.com")
        snap = snapshot.build(live=True, claude_probe_fn=_failed_probe)
        live = snap["claude"]["live"]
        assert live["source"] == "capacity-cache"
        assert live["five_hour_pct"] == 33
        assert live["stale"] is False

    def test_active_limit_survives_cached_reading(self, env_paths, monkeypatch):
        _setup(monkeypatch)
        _write_oauth_raw(env_paths["state"], pct=10, age=timedelta(hours=1))
        future = (now_local() + timedelta(hours=2)).isoformat(timespec="seconds")
        monkeypatch.setattr(
            claude,
            "transcript_limit_events",
            lambda hours=24: [
                {
                    "kind": "session-limit",
                    "reset_at": future,
                    "observed_at": iso(now_local()),
                    "text": "x",
                    "sessions": 1,
                    "count": 1,
                }
            ],
        )
        snap = snapshot.build(live=True, claude_probe_fn=_failed_probe)
        # A cached reading below 95% must NOT clear the observed limit — only a
        # same-run probe corroborates.
        assert snap["claude"]["active_limit"] is not None
        assert snap["claude"]["verdict"] == "limited"

    def test_derived_model_leads_when_everything_stale(self, env_paths, monkeypatch):
        _setup(monkeypatch)
        _write_statusline(env_paths["state"])
        reset = (now_local() + timedelta(hours=1)).isoformat(timespec="seconds")
        monkeypatch.setattr(
            claude,
            "derive_five_hour_window",
            lambda now=None, hours=48, intervals=None: {
                "window_start": iso(now_local()),
                "reset_at": reset,
                "confidence": "derived",
            },
        )
        snap = snapshot.build(live=True, claude_probe_fn=_failed_probe)
        out = render.table(snap)
        assert "5h window: no fresh reading — resets ~" in out
        assert "stale, from statusline" in out
        # The derived 5h reset leads the section and is not repeated below.
        assert out.count("derived from activity") == 1

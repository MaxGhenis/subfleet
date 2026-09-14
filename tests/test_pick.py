import argparse
import json
from datetime import timedelta

from subfleet import capacity, delegate, paths
from subfleet.snapshot import rank_for_dispatch, rank_for_spark
from subfleet.util import now_local


def entry(home, used, weekly=20, verdict="ok", account="a", primary_home=False,
          duplicate_of=None, source="live", as_of=None, weekly_reset=None,
          five_reset=None, limit_reached=False):
    return {
        "home": home,
        "is_primary_home": primary_home,
        "account_id": account,
        "email": f"{account}@x.com",
        "verdict": verdict,
        "duplicate_of": duplicate_of,
        "windows": {
            "primary": {"used_percent": used, "window_seconds": 18000, "reset_at": five_reset},
            "secondary": {"used_percent": weekly, "window_seconds": 604800, "reset_at": weekly_reset},
            "source": source,
            "as_of": as_of or now_local().isoformat(timespec="seconds"),
        },
        "recent_errors": {"usage_limit": [], "auth_revoked": []},
        "probe": {"status": "ok", "limit_reached": limit_reached},
    }


class TestRanking:
    def test_earliest_weekly_reset_wins_regardless_of_usage(self):
        soon = (now_local() + timedelta(days=1)).isoformat(timespec="seconds")
        later = (now_local() + timedelta(days=4)).isoformat(timespec="seconds")
        ranked = rank_for_dispatch([
            entry("/h/.codex-2", 5, account="a", weekly_reset=later),
            entry("/h/.codex-3", 60, account="b", weekly_reset=soon),
        ])
        assert ranked[0]["home"] == "/h/.codex-3"

    def test_app_protection_does_not_change_waterfall(self):
        soon = (now_local() + timedelta(days=1)).isoformat(timespec="seconds")
        later = (now_local() + timedelta(days=4)).isoformat(timespec="seconds")
        ranked = rank_for_dispatch([
            entry("/h/.codex", 10, account="a", primary_home=True, weekly_reset=soon),
            entry("/h/.codex-3", 15, account="b", weekly_reset=later),
        ])
        assert ranked[0]["home"] == "/h/.codex"
        assert ranked[0]["protected"] is True

    def test_handicap_argument_is_compatibility_only(self):
        soon = (now_local() + timedelta(days=1)).isoformat(timespec="seconds")
        later = (now_local() + timedelta(days=4)).isoformat(timespec="seconds")
        ranked = rank_for_dispatch([
            entry("/h/.codex", 90, account="a", primary_home=True, weekly_reset=soon),
            entry("/h/.codex-3", 1, account="b", weekly_reset=later),
        ], handicap=1000)
        assert ranked[0]["home"] == "/h/.codex"

    def test_duplicates_excluded(self):
        ranked = rank_for_dispatch([
            entry("/h/.codex-2", 5, account="a"),
            entry("/h/.codex-3", 1, account="a", duplicate_of="/h/.codex-2"),
        ])
        assert len(ranked) == 1
        assert ranked[0]["home"] == "/h/.codex-2"

    def test_exhausted_and_dead_excluded(self):
        ranked = rank_for_dispatch([
            entry("/h/.codex-2", 100, verdict="limited", account="a"),
            entry("/h/.codex-3", 40, verdict="auth-revoked", account="b"),
            entry("/h/.codex-4", 97, account="c"),  # under 5% headroom
        ])
        assert ranked == []

    def test_stale_recent_observation_qualifies_flagged(self):
        e = entry("/h/.codex-3", 10, account="b", verdict="unknown", source="observed",
                  as_of=(now_local() - timedelta(minutes=5)).isoformat(timespec="seconds"))
        e["probe"] = {"status": "network-error"}
        ranked = rank_for_dispatch([e])
        assert len(ranked) == 1
        assert ranked[0]["stale"] is True

    def test_stale_old_observation_excluded(self):
        e = entry("/h/.codex-3", 10, account="b", verdict="unknown", source="observed",
                  as_of=(now_local() - timedelta(hours=2)).isoformat(timespec="seconds"))
        ranked = rank_for_dispatch([e])
        assert ranked == []

    def test_weekly_reset_precedes_staleness_tiebreak(self):
        soon = (now_local() + timedelta(hours=8)).isoformat(timespec="seconds")
        later = (now_local() + timedelta(days=2)).isoformat(timespec="seconds")
        stale = entry("/h/.codex-2", 1, account="a", verdict="unknown", source="observed",
                      as_of=(now_local() - timedelta(minutes=5)).isoformat(timespec="seconds"),
                      weekly_reset=soon)
        live = entry("/h/.codex-3", 50, account="b", weekly_reset=later)
        ranked = rank_for_dispatch([stale, live])
        assert ranked[0]["home"] == "/h/.codex-2"

    def test_usage_does_not_break_equal_reset_tie(self):
        reset = (now_local() + timedelta(days=2)).isoformat(timespec="seconds")
        ranked = rank_for_dispatch([
            entry("/h/.codex-2", 80, weekly=80, account="a", weekly_reset=reset),
            entry("/h/.codex-3", 5, weekly=5, account="b", weekly_reset=reset),
        ])
        assert ranked[0]["home"] == "/h/.codex-2"

    def test_in_flight_is_displayed_but_never_changes_order(self):
        root = paths.runs_dir()
        root.mkdir(parents=True)

        def mark_running(name, lane):
            run_dir = root / name
            run_dir.mkdir()
            (run_dir / "meta.json").write_text(json.dumps({
                "family": "codex", "lane": lane, "finished_at": None,
            }))

        busy = "/h/.codex-2"
        idle = "/h/.codex-3"
        mark_running("20260822-100000-one", busy)
        mark_running("20260822-100001-two", busy)

        tied = rank_for_dispatch([
            entry(busy, 10, account="a"),
            entry(idle, 10, account="b"),
        ])
        assert [row["home"] for row in tied] == [busy, idle]
        assert [row["in_flight"] for row in tied] == [2, 0]

    def test_future_short_window_retry_skips_then_expiry_restores_earliest(self):
        now = now_local()
        soon = (now + timedelta(days=1)).isoformat(timespec="seconds")
        later = (now + timedelta(days=3)).isoformat(timespec="seconds")
        earliest = entry("/h/.codex-1", 25, account="a", weekly_reset=soon)
        earliest["recent_errors"]["usage_limit"] = [{
            "observed_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            "reset_at": (now + timedelta(minutes=20)).isoformat(timespec="seconds"),
            "try_again": "soon",
        }]
        fallback = entry("/h/.codex-2", 5, account="b", weekly_reset=later)
        assert rank_for_dispatch([earliest, fallback], now=now)[0]["home"] == fallback["home"]
        assert rank_for_dispatch(
            [earliest, fallback], now=now + timedelta(minutes=21)
        )[0]["home"] == earliest["home"]

    def test_wham_limit_reached_is_skipped(self):
        soon = (now_local() + timedelta(days=1)).isoformat(timespec="seconds")
        later = (now_local() + timedelta(days=2)).isoformat(timespec="seconds")
        blocked = entry(
            "/h/.codex-1", 25, account="a", weekly_reset=soon, limit_reached=True
        )
        healthy = entry("/h/.codex-2", 5, account="b", weekly_reset=later)
        assert rank_for_dispatch([blocked, healthy])[0]["home"] == healthy["home"]

    def test_past_reset_propagation_orders_identically_across_paths(self):
        now = now_local()
        past = (now - timedelta(minutes=2)).isoformat(timespec="seconds")
        future = (now + timedelta(days=1)).isoformat(timespec="seconds")
        past_home = "/h/.codex-9"
        future_home = "/h/.codex-1"

        ranked = rank_for_dispatch([
            entry(past_home, 10, account="a", weekly_reset=past),
            entry(future_home, 10, account="b", weekly_reset=future),
        ], now=now)
        assert ranked[0]["home"] == past_home

        rows = [
            {
                "family": "codex",
                "id": home,
                "home": home,
                "dispatchable": True,
                "headroom_score": 90,
                "dispatch_score": capacity._codex_dispatch_score(
                    {"reset_at": reset}, now
                ),
                "in_flight": 0,
            }
            for home, reset in ((past_home, past), (future_home, future))
        ]
        assert capacity._best_dispatchable(rows)["id"] == past_home
        assert delegate._capacity_candidates(
            {"accounts": rows}, "codex"
        )[0]["id"] == past_home


def write_protected(tmp_path, monkeypatch, payload: dict):
    cfg = tmp_path / "codex-accounts.json"
    cfg.write_text(json.dumps(payload))
    monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(cfg))


class TestProtectedAccountMetadata:
    """Protection follows identity, but no longer influences Codex ordering."""

    def test_handicap_follows_account_not_directory(self, tmp_path, monkeypatch):
        # Post-shuffle: the app account (b) now lives in .codex-3, so IT
        # carries the handicap and the primary directory does not.
        write_protected(tmp_path, monkeypatch,
                        {"protected_account": {"email": "b@x.com"}})
        ranked = rank_for_dispatch([
            entry("/h/.codex", 10, account="a", primary_home=True),
            entry("/h/.codex-3", 8, account="b"),
        ])
        assert ranked[0]["home"] == "/h/.codex"
        assert ranked[0]["protected"] is False
        assert ranked[1]["protected"] is True

    def test_account_id_match_is_case_insensitive(self, tmp_path, monkeypatch):
        write_protected(tmp_path, monkeypatch,
                        {"protected_account": {"account_id": "ACCT-B"}})
        ranked = rank_for_dispatch([
            entry("/h/.codex-2", 5, account="acct-b"),
            entry("/h/.codex-3", 9, account="c"),
        ])
        assert ranked[0]["home"] == "/h/.codex-2"
        assert ranked[0]["protected"] is True

    def test_protected_in_primary_home_matches_legacy_behavior(self, tmp_path, monkeypatch):
        write_protected(tmp_path, monkeypatch,
                        {"protected_account": {"email": "a@x.com"}})
        ranked = rank_for_dispatch([
            entry("/h/.codex", 10, account="a", primary_home=True),
            entry("/h/.codex-3", 15, account="b"),
        ])
        assert ranked[0]["home"] == "/h/.codex"
        assert ranked[0]["protected"] is True

    def test_blank_config_falls_back_to_primary_home(self, tmp_path, monkeypatch):
        write_protected(tmp_path, monkeypatch, {"protected_account": {"email": ""}})
        ranked = rank_for_dispatch([
            entry("/h/.codex", 10, account="a", primary_home=True),
            entry("/h/.codex-3", 15, account="b"),
        ])
        assert ranked[0]["home"] == "/h/.codex"
        assert ranked[0]["protected"] is True


def spark_entry(home, spark_used, *, verdict="limited", account="a", weekly=100,
                limit_reached=None, spark_limit=False, probe_status="ok",
                spark_reset=None, duplicate_of=None):
    """A lane entry whose wham probe carries the GPT-5.3-Codex-Spark bucket
    (`spark_used=None` → no bucket reported). Weekly-LIMITED by default: the
    point of the bucket is that it admits work the weekly window refuses."""
    e = entry(home, 0, weekly=weekly, verdict=verdict, account=account,
              duplicate_of=duplicate_of,
              limit_reached=(verdict == "limited") if limit_reached is None else limit_reached)
    e["probe"]["status"] = probe_status
    e["probe"]["plan_type"] = "pro"
    if spark_used is not None:
        e["probe"]["additional"] = [{
            "name": "GPT-5.3-Codex-Spark",
            "limit_reached": spark_limit,
            "primary": {"used_percent": spark_used, "window_seconds": 18000,
                        "reset_at": spark_reset},
        }]
    return e


def _snap(homes):
    n_ok = sum(1 for e in homes if e["verdict"] == "ok" and not e.get("duplicate_of"))
    return {
        "generated_at": now_local().isoformat(timespec="seconds"),
        "codex": {
            "homes": homes, "duplicates": [],
            "fleet": {"total_homes": len(homes), "dispatchable_now": n_ok,
                      "best_home": None, "earliest_reset": None},
        },
    }


class TestSparkRanking:
    def test_weekly_limited_lane_with_open_spark_bucket_qualifies(self, env_paths):
        ranked = rank_for_spark([spark_entry("/h/.codex-1", 3)])
        assert [r["home"] for r in ranked] == ["/h/.codex-1"]
        assert ranked[0]["bucket"] == "spark"
        assert ranked[0]["model"] == "gpt-5.3-codex-spark"
        assert ranked[0]["weekly_verdict"] == "limited"
        assert ranked[0]["spark_used_percent"] == 3.0

    def test_spread_lowest_spark_usage_first(self, env_paths):
        ranked = rank_for_spark([
            spark_entry("/h/.codex-1", 40, account="a"),
            spark_entry("/h/.codex-2", 10, account="b", verdict="ok", weekly=20),
            spark_entry("/h/.codex-3", 25, account="c"),
        ])
        assert [r["home"] for r in ranked] == ["/h/.codex-2", "/h/.codex-3", "/h/.codex-1"]

    def test_excludes_missing_bucket_limit_reached_floor_duplicates_and_auth_trouble(
            self, env_paths):
        ranked = rank_for_spark([
            spark_entry("/h/.codex-1", None, account="a"),                  # no bucket
            spark_entry("/h/.codex-2", 0, account="b", spark_limit=True),   # bucket at limit
            spark_entry("/h/.codex-3", 97, account="c"),                    # below headroom floor
            spark_entry("/h/.codex-4", 1, account="d", duplicate_of="/h/.codex-9"),
            spark_entry("/h/.codex-5", 1, account="e", verdict="auth-revoked",
                        probe_status="token-revoked"),
            spark_entry("/h/.codex-6", 1, account="f"),
        ])
        assert [r["home"] for r in ranked] == ["/h/.codex-6"]

    def test_weekly_waterfall_is_unchanged_by_the_bucket(self, env_paths):
        assert rank_for_dispatch([spark_entry("/h/.codex-1", 3)]) == []


class TestPickSparkCli:
    def _args(self, **kw):
        base = dict(family="codex", model="spark", json=False, all=False, cached=True,
                    min_headroom=None, handicap=10.0, no_handicap=False, exclude=[])
        base.update(kw)
        return argparse.Namespace(**base)

    def test_prints_the_least_used_spark_bucket(self, env_paths, monkeypatch, capsys):
        from subfleet import cli
        homes = [spark_entry("/h/.codex-1", 30, account="a"),
                 spark_entry("/h/.codex-2", 5, account="b")]
        monkeypatch.setattr(cli, "_load_snapshot", lambda cached: _snap(homes))
        assert cli.cmd_pick(self._args()) == 0
        out, err = capsys.readouterr()
        assert out.strip() == "/h/.codex-2"
        assert "spark 5h 5% used" in err and "(limited)" in err

    def test_json_reports_bucket_ranking_and_exclusions(self, env_paths, monkeypatch, capsys):
        from subfleet import cli
        homes = [spark_entry("/h/.codex-1", 30, account="a"),
                 spark_entry("/h/.codex-2", 0, account="b", spark_limit=True)]
        monkeypatch.setattr(cli, "_load_snapshot", lambda cached: _snap(homes))
        assert cli.cmd_pick(self._args(json=True, all=True)) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["bucket"] == "spark" and data["model"] == "gpt-5.3-codex-spark"
        assert data["best"] == "/h/.codex-1"
        assert [r["home"] for r in data["ranked"]] == ["/h/.codex-1"]
        assert data["excluded"][0]["home"] == "/h/.codex-2"
        assert data["excluded"][0]["spark"]["limit_reached"] is True

    def test_rc1_when_no_bucket_is_open(self, env_paths, monkeypatch, capsys):
        from subfleet import cli
        homes = [spark_entry("/h/.codex-1", 0, account="a", spark_limit=True)]
        monkeypatch.setattr(cli, "_load_snapshot", lambda cached: _snap(homes))
        assert cli.cmd_pick(self._args()) == 1
        assert "no codex home with an open Spark 5h bucket" in capsys.readouterr().err

    def test_full_model_id_also_selects_the_bucket(self, env_paths, monkeypatch, capsys):
        from subfleet import cli
        homes = [spark_entry("/h/.codex-1", 2, account="a")]
        monkeypatch.setattr(cli, "_load_snapshot", lambda cached: _snap(homes))
        assert cli.cmd_pick(self._args(model="gpt-5.3-codex-spark")) == 0
        assert capsys.readouterr().out.strip() == "/h/.codex-1"

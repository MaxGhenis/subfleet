"""GPT-5.3-Codex-Spark meters on its own 5h bucket (wham additional_rate_limits)
and is admitted on weekly-limited lanes — verified live 2026-09-10."""
from datetime import datetime, timedelta, timezone

from subfleet import capacity, claude, codex, render

from test_pick import entry, spark_entry
from test_watchdog import snap

NOW = datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)
SPARK_RESET = (NOW + timedelta(hours=4)).isoformat()


def probe(weekly_used, *, limit_reached, spark_used=0, spark_limit=False, plan="pro",
          bucket=True):
    result = {
        "status": "ok", "allowed": not limit_reached, "limit_reached": limit_reached,
        "plan_type": plan,
        "primary": {"used_percent": weekly_used, "window_seconds": 604800,
                    "reset_at": (NOW + timedelta(days=5)).isoformat()},
        "secondary": None,
        "additional": [],
    }
    if bucket:
        result["additional"].append({
            "name": "GPT-5.3-Codex-Spark", "limit_reached": spark_limit,
            "primary": {"used_percent": spark_used, "window_seconds": 18000,
                        "reset_at": SPARK_RESET},
        })
    return result


def test_spark_bucket_is_read_from_the_additional_limits():
    assert codex.spark_bucket(probe(100, limit_reached=True, spark_used=12)) == {
        "used_percent": 12, "window_seconds": 18000, "reset_at": SPARK_RESET,
        "limit_reached": False,
    }
    assert codex.spark_bucket(probe(0, limit_reached=False, bucket=False)) is None
    assert codex.spark_bucket(None) is None


def test_spark_open_is_independent_of_the_weekly_verdict():
    assert codex.spark_open(probe(100, limit_reached=True, spark_used=12)) is True
    assert codex.spark_open(probe(100, limit_reached=True, spark_limit=True)) is False
    assert codex.spark_open(probe(0, limit_reached=False, spark_used=96)) is False
    assert codex.spark_open(probe(0, limit_reached=False, spark_used=96), min_headroom=2) is True
    assert codex.spark_open(probe(0, limit_reached=False, bucket=False)) is False
    assert codex.spark_open(probe(0, limit_reached=False, plan="free")) is False
    assert codex.spark_open({"status": "http-401"}) is False


def test_is_spark_model_accepts_alias_and_full_id():
    assert codex.is_spark_model("spark")
    assert codex.is_spark_model("gpt-5.3-codex-spark")
    assert codex.is_spark_model("GPT-5.3-Codex-Spark")
    assert not codex.is_spark_model("gpt-6-astra")
    assert not codex.is_spark_model(None)


def test_capacity_row_marks_a_weekly_limited_lane_spark_dispatchable(
        monkeypatch, env_paths, codex_home_factory):
    make_home, _ = codex_home_factory
    home = make_home("codex-one", "acct-1", "codex@x.com")
    monkeypatch.setenv("SUBFLEET_CODEX_HOMES", str(home))
    monkeypatch.setattr(claude, "identity", lambda: {})
    monkeypatch.setattr(codex, "probe_all", lambda auths, timeout=15: [
        probe(100, limit_reached=True, spark_used=12)
    ])
    data = capacity.collect(force_refresh=True, now=NOW)
    row = next(r for r in data["accounts"] if r["family"] == "codex")
    assert row["status"] == "limited"
    assert row["dispatchable"] is False
    assert row["spark"]["used_percent"] == 12
    assert row["spark_dispatchable"] is True


def test_capacity_row_without_the_bucket_is_not_spark_dispatchable(
        monkeypatch, env_paths, codex_home_factory):
    make_home, _ = codex_home_factory
    home = make_home("codex-one", "acct-1", "codex@x.com")
    monkeypatch.setenv("SUBFLEET_CODEX_HOMES", str(home))
    monkeypatch.setattr(claude, "identity", lambda: {})
    monkeypatch.setattr(codex, "probe_all", lambda auths, timeout=15: [
        probe(10, limit_reached=False, bucket=False)
    ])
    data = capacity.collect(force_refresh=True, now=NOW)
    row = next(r for r in data["accounts"] if r["family"] == "codex")
    assert row["dispatchable"] is True
    assert row["spark"] is None
    assert row["spark_dispatchable"] is False


def test_codex_table_shows_the_spark_column_and_fleet_open_count(env_paths):
    with_bucket = spark_entry("/h/.codex-2", 7, account="with-spark")
    no_bucket = entry("/h/.codex-3", 10, account="no-spark")
    s = snap([with_bucket, no_bucket])
    s["codex"]["fleet"]["spark_open"] = 1
    out = render.table(s)
    lines = out.splitlines()
    assert any("spark5h" in line for line in lines)
    with_row = next(line for line in lines if "with-spark@x.com" in line)
    no_row = next(line for line in lines if "no-spark@x.com" in line)
    assert "7%" in with_row
    assert "7%" not in no_row
    assert "spark open: 1/2" in out

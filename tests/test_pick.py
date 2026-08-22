from datetime import timedelta

from carpool import run_ledger
from carpool.snapshot import rank_for_dispatch
from carpool.util import now_local


def entry(home, used, weekly=20, verdict="ok", account="a", primary_home=False,
          duplicate_of=None, source="live", as_of=None):
    return {
        "home": home,
        "is_primary_home": primary_home,
        "account_id": account,
        "email": f"{account}@example.com",
        "verdict": verdict,
        "duplicate_of": duplicate_of,
        "windows": {
            "primary": {"used_percent": used, "window_seconds": 18000, "reset_at": None},
            "secondary": {"used_percent": weekly, "window_seconds": 604800, "reset_at": None},
            "source": source,
            "as_of": as_of or now_local().isoformat(timespec="seconds"),
        },
        "recent_errors": {"usage_limit": [], "auth_revoked": []},
        "probe": {"status": "ok"},
    }


class TestRanking:
    def test_lowest_usage_wins(self):
        ranked = rank_for_dispatch([
            entry("/h/.codex-2", 60, account="a"),
            entry("/h/.codex-3", 5, account="b"),
        ])
        assert ranked[0]["home"] == "/h/.codex-3"

    def test_primary_handicap_spares_main_account(self):
        ranked = rank_for_dispatch([
            entry("/h/.codex-1", 10, account="a", primary_home=True),
            entry("/h/.codex-3", 15, account="b"),
        ])
        # 10+10 handicap > 15, so the alternate wins despite higher raw usage.
        assert ranked[0]["home"] == "/h/.codex-3"

    def test_no_handicap_ranks_raw(self):
        ranked = rank_for_dispatch([
            entry("/h/.codex-1", 10, account="a", primary_home=True),
            entry("/h/.codex-3", 15, account="b"),
        ], handicap=0)
        assert ranked[0]["home"] == "/h/.codex-1"

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

    def test_live_beats_stale_regardless_of_score(self):
        stale = entry("/h/.codex-2", 1, account="a", verdict="unknown", source="observed",
                      as_of=(now_local() - timedelta(minutes=5)).isoformat(timespec="seconds"))
        live = entry("/h/.codex-3", 50, account="b")
        ranked = rank_for_dispatch([stale, live])
        assert ranked[0]["home"] == "/h/.codex-3"

    def test_weekly_tiebreak(self):
        ranked = rank_for_dispatch([
            entry("/h/.codex-2", 10, weekly=80, account="a"),
            entry("/h/.codex-3", 10, weekly=5, account="b"),
        ])
        assert ranked[0]["home"] == "/h/.codex-3"

    def test_in_flight_is_a_same_capacity_tiebreak(self, monkeypatch):
        monkeypatch.setattr(
            run_ledger,
            "in_flight_counts",
            lambda: {("codex", "/h/.codex-2"): 2},
        )
        ranked = rank_for_dispatch([
            entry("/h/.codex-2", 10, weekly=5, account="a"),
            entry("/h/.codex-3", 10, weekly=5, account="b"),
        ])
        assert [row["home"] for row in ranked] == ["/h/.codex-3", "/h/.codex-2"]
        assert ranked[1]["in_flight"] == 2

    def test_better_headroom_beats_in_flight_count(self, monkeypatch):
        monkeypatch.setattr(
            run_ledger,
            "in_flight_counts",
            lambda: {("codex", "/h/.codex-2"): 5},
        )
        ranked = rank_for_dispatch([
            entry("/h/.codex-2", 5, account="a"),
            entry("/h/.codex-3", 10, account="b"),
        ])
        assert ranked[0]["home"] == "/h/.codex-2"

"""Invariants of the aggregation helpers behind docs/native-workflows-vs-subfleet.md.

The script publishes percentiles, peak concurrency and error-class counts, so
its pure helpers are checked here for every input a seeded generator draws:
bounds and monotonicity, agreement with a reference implementation, and a
partition of worker errors into labels that never carry the plain provider
phrases subfleet v2's Claude classifier matches.
"""
import importlib.util
import random
import re
import statistics
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docs" / "native-workflows-vs-subfleet.ledger-stats.py"
spec = importlib.util.spec_from_file_location("nwvs_stats", SCRIPT)
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)

SEEDS = range(200)
# The plain two-word phrases the v2 classifier treats as a lane fault
# (subfleet-v2 b053e3de, subfleet/adapters/claude.py LIMIT_RE), built here so
# this file does not contain them either.
PLAIN_LIMIT = re.compile(
    r"(?:hit\syour|reached\syour|usage|session|weekly|5-hour|subscription)\slimit|limit\sreached",
    re.I,
)


def draw(rng, n_max=40, lo=0, hi=1000):
    return [rng.randint(lo, hi) for _ in range(rng.randint(1, n_max))]


@pytest.mark.parametrize("seed", SEEDS)
def test_pct_is_bounded_monotone_and_hits_the_extremes(seed):
    rng = random.Random(seed)
    xs = draw(rng)
    qs = sorted(rng.uniform(0, 100) for _ in range(8))
    values = [stats.pct(xs, q) for q in qs]
    assert all(min(xs) <= v <= max(xs) for v in values)
    assert values == sorted(values)
    assert stats.pct(xs, 0) == min(xs)
    assert stats.pct(xs, 100) == max(xs)


@pytest.mark.parametrize("seed", SEEDS)
def test_pct_agrees_with_statistics_inclusive_quantiles(seed):
    rng = random.Random(seed)
    xs = draw(rng)
    if len(xs) < 2:
        return
    reference = statistics.quantiles(xs, n=10, method="inclusive")
    for k, expected in enumerate(reference, start=1):
        assert stats.pct(xs, 10 * k) == pytest.approx(expected)


def test_pct_of_nothing_is_none():
    assert stats.pct([], 50) is None


def brute_force_overlap(intervals):
    """Most half-open intervals [start, end) covering one instant."""
    starts = {s for s, _ in intervals}
    return max((sum(1 for s, e in intervals if s <= t < e) for t in starts), default=0)


@pytest.mark.parametrize("seed", SEEDS)
def test_max_overlap_matches_a_brute_force_count(seed):
    rng = random.Random(seed)
    intervals = []
    for _ in range(rng.randint(0, 30)):
        start = rng.randint(0, 200)
        intervals.append((start, start + rng.randint(0, 60)))
    got = stats.max_overlap(intervals)
    assert got == brute_force_overlap(intervals)
    assert 0 <= got <= len(intervals)


def test_max_overlap_treats_touching_intervals_as_sequential():
    assert stats.max_overlap([(0, 10), (10, 20)]) == 1
    assert stats.max_overlap([(0, 10), (9, 20)]) == 2


@pytest.mark.parametrize("seed", SEEDS)
def test_error_classes_partition_and_never_use_plain_trigger_phrases(seed):
    rng = random.Random(seed)
    words = ["You've", "hit", "your", "session", "weekly", "usage", "rate", "limit", "resets",
             "Oct", "2", "at", "11am", "API", "Error:", "overloaded", "rate_limit", "\n", "  "]
    text = " ".join(rng.choice(words) for _ in range(rng.randint(0, 12)))
    label = stats.error_class(text)
    assert label in {"session-limit", "weekly-limit", "other plan-limit or rate-limit", "other"}
    assert not PLAIN_LIMIT.search(label)


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("You've hit your " + "session" + " limit · resets 4am", "session-limit"),
        ("You've hit your " + "weekly" + "\tlimit · resets Oct 2", "weekly-limit"),
        ("API Error: rate_limit_error", "other plan-limit or rate-limit"),
        ("API Error: overloaded", "other"),
        (None, "other"),
    ],
)
def test_error_class_examples(text, label):
    assert stats.error_class(text) == label


def test_row_strips_email_addresses(capsys):
    stats.row("lane", "claude-4 <someone@example.org> 2 in flight")
    out = capsys.readouterr().out
    assert "@" not in out
    assert "<email>" in out


def test_version_tuple_orders_releases_and_rejects_junk():
    assert stats.version_tuple("2.1.271") < stats.version_tuple("2.1.280")
    assert stats.version_tuple("2.1.9") < stats.version_tuple("2.1.10")
    assert stats.version_tuple("dev") is None

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from subfleet import delegate


def capacity_row(family, account, score=80.0, *, dispatchable=True,
                 limited_until=None, confidence="live", status=None,
                 scoped_limits=None, in_flight=0):
    return {
        "family": family,
        "id": account,
        "email": account if family == "claude" else f"{Path(account).name}@x",
        "five_hour": {
            "used_percent": None if score is None else 100 - score,
            "tokens": 0 if family == "claude" else None,
            "capacity": None,
            "reset_at": None,
        },
        "weekly": {
            "used_percent": None if score is None else 100 - score,
            "tokens": 0 if family == "claude" else None,
            "capacity": None,
            "reset_at": None,
        },
        "scoped_limits": list(scoped_limits or []),
        "learned_capacity": None,
        "limited_until": limited_until,
        "confidence": confidence,
        "status": status or ("ok" if dispatchable else "limited"),
        "dispatchable": dispatchable,
        "headroom_score": score if dispatchable else 0.0,
        "dispatch_score": score if dispatchable else 0.0,
        "enrolled": family == "claude",
        "in_flight": in_flight,
    }


def capacity_snapshot(codex_rows=None, claude_rows=None):
    if codex_rows is None:
        codex_rows = [capacity_row("codex", "/home/codex")]
    if claude_rows is None:
        claude_rows = [
            capacity_row("claude", email, score=None, confidence="estimated")
            for email in ("a@x", "b@x", "c@x", "d@x")
        ]
    accounts = [*codex_rows, *claude_rows]
    families = {}
    for family, rows in (("codex", codex_rows), ("claude", claude_rows)):
        available = [row for row in rows if row["dispatchable"]]
        known = [row for row in available if row["headroom_score"] is not None]
        best = max(known, key=lambda row: row["headroom_score"]) if known else (
            available[0] if available else None
        )
        resets = [row["limited_until"] for row in rows if row.get("limited_until")]
        all_limited = bool(rows) and all(
            row["status"] in {"limited", "exhausted"} for row in rows
        )
        families[family] = {
            "available": bool(available),
            "all_limited": all_limited,
            "state": "available" if available else ("limited" if all_limited else "empty"),
            "headroom_score": (
                best["headroom_score"] if best else (0.0 if all_limited else None)
            ),
            "best": best["id"] if best else None,
            "dispatchable": len(available),
            "accounts": len(rows),
            "earliest_reset": min(resets) if resets else None,
        }
    return {
        "generated_at": "2026-07-22T12:00:00-04:00",
        "cache": {"hit": True, "ttl_seconds": 120},
        "accounts": accounts,
        "families": families,
    }


@pytest.mark.parametrize(("prompt", "kind", "model"), [
    ("Draft an email as Max", "fable", "fable"),
    ("Use Max's voice", "fable", "fable"),
    ("Publish a blog post", "fable", "fable"),
    ("Write an essay", "fable", "fable"),
    ("Polish this prose", "fable", "fable"),
    ("Recommend a strategy", "fable", "fable"),
    ("Adjudicate the dispute", "fable", "fable"),
    ("Give the verdict", "fable", "fable"),
    ("Open the merge gate", "fable", "fable"),
    ("Launch this", "fable", "fable"),
    ("Send this", "fable", "fable"),
    ("WDYT about the design?", "fable", "fable"),
    ("Review this patch", "review", "opus"),
    ("Assess this patch", "review", "opus"),
    ("Critique this patch", "review", "opus"),
    ("Audit this patch", "review", "opus"),
    ("Evaluate this patch", "review", "opus"),
    ("Referee this dispute", "review", "opus"),
    ("For each file, check imports", "sweep", "terra"),
    ("Extract ids across all rows", "sweep", "terra"),
    ("Count a batch of records", "sweep", "terra"),
    ("For each feature implement it", "build", "opus"),
    ("Implement the endpoint", "build", "opus"),
    ("Fix and test the bug", "build", "opus"),
    ("Refactor the parser", "build", "opus"),
])
def test_routing_table(prompt, kind, model):
    got, _ = delegate.classify(prompt)
    assert got == kind
    assert delegate.choose_model(got) == model


def test_overrides_and_claude_models_only_explicit():
    kind, _ = delegate.classify("review this", "build")
    assert (kind, delegate.choose_model(kind, "terra")) == ("build", "terra")
    assert delegate.choose_model("fable") == "fable"
    assert delegate.choose_model("review") == "opus"
    assert delegate.choose_model("build") == "opus"
    assert delegate.choose_model("build", "sol") == "astra"  # retired alias
    assert delegate.choose_model("build", "haiku") == "haiku"
    assert delegate.choose_model("build", "opus") == "opus"
    assert delegate.MODEL_NAMES["opus"] == "claude-opus-5"


@pytest.mark.parametrize(
    ("task", "tier", "model"),
    [
        *((task, "trivial", "luna") for task in ("lookup", "research", "review", "build")),
        *((task, "easy", "luna") for task in ("lookup", "research", "review", "build")),
        *((task, "standard", "opus") for task in ("lookup", "research", "review", "build")),
        *((task, "hard", "astra") for task in ("lookup", "research", "review", "build")),
        ("sweep", "trivial", "terra"),
        ("sweep", "easy", "terra"),
        ("sweep", "standard", "terra"),
        ("sweep", "hard", "astra"),
        *((task, tier, "fable") for task in delegate.FABLE_TASKS for tier in delegate.TIERS),
    ],
)
def test_semantic_task_tier_grid(task, tier, model):
    assert delegate.choose_model(task, tier=tier) == model
    assert delegate.semantic_model_candidates(task, tier)[0] == model


def test_semantic_upward_model_chains_are_explicit():
    assert delegate.semantic_model_candidates("lookup", "trivial") == (
        "luna", "opus", "astra",
    )
    assert delegate.semantic_model_candidates("review", "easy") == (
        "luna", "opus", "astra",
    )
    assert delegate.semantic_model_candidates("build", "standard") == ("opus", "astra")
    assert delegate.semantic_model_candidates("research", "hard") == ("astra",)


@pytest.mark.parametrize(
    "argv",
    [
        ["--task", "lookup", "find x"],
        ["--tier", "easy", "find x"],
        ["-t", "build", "--task", "lookup", "--tier", "easy", "find x"],
    ],
)
def test_semantic_task_tier_misuse_is_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        delegate.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize(("prompt", "kind"), [
    ("Final review and implement it", "fable"),
    ("Design, review, and implement it", "fable"),
    ("Review and implement it", "review"),
    ("Audit each file and check imports", "review"),
])
def test_routing_precedence(prompt, kind):
    got, _ = delegate.classify(prompt)
    assert got == kind


@pytest.mark.parametrize("prompt", [
    "Write this function",
    "Draft the implementation plan",
    "Judge this change",
    "Decide what comes next",
    "Recommend improvements",
    "Preview the implementation",
])
def test_removed_floor_signals_and_word_boundaries_default_to_build(prompt):
    got, signals = delegate.classify(prompt)
    assert got == "build"
    assert not signals["fable"]
    assert not signals["review"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    state = tmp_path / "state"
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"enrolled": {"a@x": "a", "b@x": "b", "c@x": "c", "d@x": "d"}}))
    monkeypatch.setenv("DELEGATE_STATE_DIR", str(state))
    monkeypatch.setenv("DELEGATE_ACCOUNTS_FILE", str(accounts))
    monkeypatch.setattr(delegate, "_active_desktop_email", lambda: None)
    monkeypatch.setattr(delegate, "_capacity_report", capacity_snapshot)
    return state


@pytest.mark.parametrize(
    "extra",
    [
        ["--independent-review", "-s", "read-only"],
        ["--review-root", "{root}", "-s", "read-only"],
        ["--independent-review", "--review-root", "{root}"],
        ["--independent-review", "--review-root", "{root}", "-s", "workspace-write"],
        ["--independent-review", "--review-root", "{root}/missing", "-s", "read-only"],
        ["--independent-review", "--review-root", "{root}", "-s", "read-only", "-b", "salvage"],
    ],
)
def test_independent_review_validation_precedes_capacity(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: pytest.fail("invalid review queried capacity")
    )
    expanded = [value.replace("{root}", str(tmp_path)) for value in extra]
    with pytest.raises(SystemExit) as exc:
        delegate.main([*expanded, "review the artifact"])
    assert exc.value.code == 2


@pytest.mark.parametrize("model", ["astra", "fable"])
def test_independent_review_passes_canonical_read_root_to_runner(
    isolated, tmp_path, monkeypatch, model
):
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    source = tmp_path / "source with spaces"
    source.mkdir()
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    calls = []
    monkeypatch.setattr(
        delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls)
    )

    assert delegate.main([
        "-m", model, "-t", "review", "-C", str(neutral), "-s", "read-only",
        "--independent-review", "--review-root", str(alias), "--no-preamble",
        "review the artifact", "-o", str(isolated / "review.md"),
    ]) == 0

    assert len(calls) == 1
    command = calls[0]
    assert "-I" in command
    assert command[command.index("-D") + 1] == str(source.resolve())
    assert command[command.index("-C") + 1] == str(neutral)
    assert command[command.index("-s") + 1] == "read-only"
    assert command[command.index("-m") + 1] == delegate.MODEL_NAMES[model]
    assert "-b" not in command
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["overrides"]["independent_review"] is True
    assert decision["overrides"]["review_root"] == str(alias)


def test_rotation_persists_skips_cooldown_and_expiry(isolated):
    assert delegate.pick_fable_lane() == "a@x"
    assert delegate.pick_fable_lane() == "b@x"
    delegate.record_cooldown("c@x", delegate._now() + timedelta(hours=1))
    assert delegate.pick_fable_lane() == "d@x"
    cooldowns = delegate._load_cooldowns()
    cooldowns["c@x"]["*"] = (
        delegate._now() - timedelta(seconds=1)
    ).isoformat()
    delegate._save_cooldowns(cooldowns)
    assert delegate.pick_fable_lane() == "a@x"
    assert json.loads((isolated / "rotation.json").read_text())["last_used"] == "a@x"


def test_capacity_report_uses_delegate_accounts_override(tmp_path, monkeypatch):
    roster = tmp_path / "delegate-accounts.json"
    roster.write_text("{}")
    monkeypatch.setenv("DELEGATE_ACCOUNTS_FILE", str(roster))
    seen = {}

    def report(**kwargs):
        seen.update(kwargs)
        return {"accounts": [], "families": {}}

    monkeypatch.setattr(delegate.capacity, "report", report)
    assert delegate._capacity_report() == {"accounts": [], "families": {}}
    assert seen["accounts_file"] == roster


def test_capacity_candidates_use_same_dispatch_order_as_family_summary():
    active = capacity_row("claude", "active@x", score=90)
    active.update({"active": True, "dispatch_score": 80})
    active["five_hour"]["tokens"] = active["weekly"]["tokens"] = 1
    alternate = capacity_row("claude", "alternate@x", score=80)
    alternate["dispatch_score"] = 80
    alternate["five_hour"]["tokens"] = alternate["weekly"]["tokens"] = 10

    rows = [alternate, active]
    # The desktop login is the last resort even at an equal score with fewer
    # tokens (Max, 2026-09-04): the alternate wins in both orderings, and the
    # login still serves when it is alone.
    assert delegate.capacity.family_summaries(rows)["claude"]["best"] == "alternate@x"
    assert delegate._capacity_candidates({"accounts": rows}, "claude")[0]["id"] == "alternate@x"
    assert delegate.capacity.family_summaries([active])["claude"]["best"] == "active@x"
    assert delegate._capacity_candidates({"accounts": [active]}, "claude")[0]["id"] == "active@x"


def test_codex_capacity_candidates_ignore_in_flight_after_dispatch_score():
    busy = capacity_row("codex", "/busy", score=80, in_flight=2)
    idle = capacity_row("codex", "/idle", score=80, in_flight=0)
    assert delegate._capacity_candidates({"accounts": [busy, idle]}, "codex")[0]["id"] == "/busy"

    busy["dispatch_score"] = 81
    assert delegate._capacity_candidates({"accounts": [busy, idle]}, "codex")[0]["id"] == "/busy"


def fable_limit(reset_at, *, percent=100, severity="critical"):
    return {
        "kind": "weekly_scoped",
        "group": "weekly",
        "percent": percent,
        "severity": severity,
        "resets_at": reset_at,
        "is_active": True,
        "scope_model": "Fable",
        "scope_surface": None,
    }


def model_limit(model, reset_at="2026-07-28T04:59:59Z"):
    limit = fable_limit(reset_at)
    limit["scope_model"] = model
    return limit


def test_capacity_candidates_skip_only_the_matching_model_scope():
    blocked = capacity_row(
        "claude",
        "blocked@x",
        score=90,
        scoped_limits=[fable_limit("2026-07-28T04:59:59Z")],
    )
    healthy = capacity_row("claude", "healthy@x", score=80)
    data = {"accounts": [blocked, healthy]}

    assert delegate._capacity_candidates(
        data, "claude", model_family="Fable"
    )[0]["id"] == "healthy@x"
    assert delegate._capacity_candidates(
        data, "claude", model_family="Opus"
    )[0]["id"] == "blocked@x"


def test_capacity_candidates_rank_by_requested_model_headroom():
    generic_best = capacity_row("claude", "generic-best@x", score=90)
    generic_best["model_windows"] = {
        "fable": {"used_percent": 90, "reset_at": None},
        "opus": {"used_percent": 10, "reset_at": None},
    }
    fable_best = capacity_row("claude", "fable-best@x", score=80)
    fable_best["model_windows"] = {
        "fable": {"used_percent": 20, "reset_at": None},
        "opus": {"used_percent": 90, "reset_at": None},
    }
    data = {"accounts": [generic_best, fable_best]}

    assert delegate._capacity_candidates(
        data, "claude", model_family="Fable"
    )[0]["id"] == "fable-best@x"
    assert delegate._capacity_candidates(
        data, "claude", model_family="Opus"
    )[0]["id"] == "generic-best@x"


@pytest.mark.parametrize(
    ("tier", "blocked_models", "requested", "selected", "runner_model"),
    [
        # trivial and easy now route to Luna (Codex), so a Claude scoped limit
        # cannot move them; see the two Luna tests below.
        ("standard", ["Opus"], "opus", "astra", "gpt-6-astra"),
    ],
)
def test_semantic_capacity_fallback_only_moves_upward(
    isolated, monkeypatch, capsys, tier, blocked_models, requested, selected, runner_model
):
    lane = capacity_row(
        "claude", "lane@x", score=90,
        scoped_limits=[model_limit(model) for model in blocked_models],
    )
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(claude_rows=[lane]),
    )
    calls = []
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        fake_run_factory([(0, "", "")], calls),
    )

    assert delegate.main([
        "--task", "build", "--tier", tier, "implement x",
        "-o", str(isolated / f"{tier}.md"),
    ]) == 0

    assert calls[0][calls[0].index("-m") + 1] == runner_model
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["task"] == "build" and decision["tier"] == tier
    assert decision["requested_model"] == requested
    assert decision["model"] == selected
    assert decision["routing_capacity_states"][requested] is False
    assert "CAPABILITY FALLBACK" in capsys.readouterr().err


def test_semantic_runtime_limits_promote_to_next_model(
    isolated, monkeypatch, capsys
):
    lanes = [
        capacity_row("claude", f"lane-{index}@x", score=90)
        for index in range(3)
    ]
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(claude_rows=lanes),
    )
    calls = []
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        fake_run_factory(
            [(4, "", "usage limit")] * 3 + [(0, "", "")] * 3, calls
        ),
    )

    assert delegate.main([
        "--task", "lookup", "--tier", "trivial", "find x",
        "-o", str(isolated / "runtime-fallback.md"),
    ]) == 0

    models = [cmd[cmd.index("-m") + 1] for cmd in calls]
    # trivial starts on Luna (the snapshot's one Codex home); a runtime usage
    # limit there promotes upward to Opus on the Claude lanes.
    assert models[0] == delegate.MODEL_NAMES["luna"]
    assert models[1:] and all(m == delegate.MODEL_NAMES["opus"] for m in models[1:])
    decisions = [
        json.loads(line)
        for line in (isolated / "decisions.jsonl").read_text().splitlines()
    ]
    assert [decision["result"] for decision in decisions][-1] == 0
    assert all(decision["result"] == 4 for decision in decisions[:-1])
    assert decisions[-1]["requested_model"] == "luna"
    assert decisions[-1]["model"] == "opus"
    assert decisions[-1]["routing_capacity_states"]["luna"] is False
    assert decisions[-1]["routing_history"] == [{
        "from": "luna",
        "to": "opus",
        "reason": "exhausted at runtime",
    }]
    assert "routing lookup/trivial upward to opus" in capsys.readouterr().err


def test_semantic_sandbox_and_luna_tiers(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        fake_run_factory([(0, "", ""), (0, "", "")], calls),
    )

    assert delegate.main([
        "--task", "lookup", "--tier", "trivial", "find x",
        "-o", str(isolated / "lookup.md"),
    ]) == 0
    assert delegate.main([
        "--task", "build", "--tier", "easy", "implement x",
        "-o", str(isolated / "build.md"),
    ]) == 0

    assert calls[0][calls[0].index("-m") + 1] == delegate.MODEL_NAMES["luna"]
    assert calls[0][calls[0].index("-s") + 1] == "read-only"
    assert calls[1][calls[1].index("-m") + 1] == delegate.MODEL_NAMES["luna"]
    assert calls[1][calls[1].index("-s") + 1] == "workspace-write"
    assert delegate.MODEL_NAMES["sonnet"] == "sonnet"  # the alias still names itself
    assert all("-I" not in command and "-D" not in command for command in calls)
    assert delegate.MODEL_FAMILY["sonnet"] == "claude"
    assert delegate.capacity.normalize_claude_model("sonnet") == "sonnet"


def test_explicit_model_override_is_exact_for_semantic_task(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        fake_run_factory([(0, "", "")], calls),
    )

    assert delegate.main([
        "--task", "build", "--tier", "hard", "-m", "sonnet",
        "implement x", "-o", str(isolated / "override.md"),
    ]) == 0

    assert calls[0][calls[0].index("-m") + 1] == "sonnet"
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["requested_model"] == "sonnet"
    assert decision["model"] == "sonnet"
    assert decision["routing_capacity_states"] is None


def test_hard_semantic_task_does_not_downgrade_astra(isolated, monkeypatch, capsys):
    exhausted = [capacity_row("codex", "/home/full", dispatchable=False)]
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(codex_rows=exhausted),
    )
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("hard Astra task was downgraded"),
    )

    assert delegate.main([
        "--task", "review", "--tier", "hard", "review x",
        "-o", str(isolated / "hard.md"),
    ]) == 3

    assert "CROSS-FAMILY" not in capsys.readouterr().err
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["requested_model"] == decision["model"] == "astra"


def test_semantic_authored_prose_keeps_fable_floor(
    isolated, monkeypatch, capsys
):
    limited = [capacity_row("claude", "full@x", dispatchable=False)]
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(claude_rows=limited),
    )
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Fable floor was downgraded"),
    )

    assert delegate.main([
        "--task", "authored-prose", "--tier", "hard", "polish this",
        "-o", str(isolated / "prose.md"),
    ]) == 3

    assert "FABLE FLOOR" in capsys.readouterr().err
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["requested_model"] == decision["model"] == "fable"


def fake_run_factory(results, calls):
    def run(cmd, **kwargs):
        calls.append(cmd)
        if "status" in cmd:
            return CompletedProcess(cmd, 1, "", "")
        value = results.pop(0)
        return CompletedProcess(cmd, *value)
    return run


def test_rc4_cooldown_rotates_and_three_attempt_cap(isolated, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(4, "", "limit resets 3:15pm")]*3, calls))
    assert delegate.main(["-m", "fable", "task", "-o", str(isolated / "out")]) == 3
    assert len(calls) == 3
    cooldowns = json.loads((isolated / "cooldowns.json").read_text())
    assert len(cooldowns) == 3
    assert all(set(scopes) == {"claude-fable-5-1"} for scopes in cooldowns.values())
    assert len((isolated / "decisions.jsonl").read_text().splitlines()) == 3


def test_rc4_preserves_precise_cooldown_written_by_runner_hook(
        isolated, tmp_path, monkeypatch):
    from test_claude_lane_script import SCRIPT as CLAUDE_LANE, _fixture_env

    lane_root = tmp_path / "near-reset-lane"
    lane_root.mkdir()
    reset_at = delegate._now() + timedelta(minutes=10)
    reset_label = reset_at.strftime("%-I:%M%p").lower()
    runner_env, run_paths = _fixture_env(
        lane_root,
        f"printf '%s\\n' '{{\"is_error\":true,\"result\":\"session limit; resets {reset_label}\"}}'\nexit 9\n",
    )
    for key in (
        "PATH", "CLAUDE_LANE_CLAUDE", "CLAUDE_LANE_AGENT_SECRET",
        "CLAUDE_LANE_CLAUDE_MODEL", "CLAUDE_LANE_BACKOFF", "UUID_COUNTER",
    ):
        monkeypatch.setenv(key, runner_env[key])
    monkeypatch.setenv("CLAUDE_LANE_SUBFLEET", str(CLAUDE_LANE.parent / "subfleet"))
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(tmp_path / "capacity-state"))
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("DELEGATE_CLAUDE_LANE", str(CLAUDE_LANE))

    rc = delegate.main([
        "-m", "fable", "-a", "a@x", "-C", str(run_paths["workdir"]),
        "-o", str(tmp_path / "out.md"), "task",
    ])

    assert rc == 3
    saved = datetime.fromisoformat(
        json.loads((isolated / "cooldowns.json").read_text())["a@x"][
            "claude-fable-5-1"
        ]
    )
    expected = reset_at.replace(second=0, microsecond=0)
    assert abs((saved - expected).total_seconds()) < 2


def test_rc5_long_cooldown_and_ritual(isolated, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(5, "", "dead")]*3, calls))
    assert delegate.main(["-m", "fable", "task", "-o", str(isolated / "out")]) == 3
    err = capsys.readouterr().err
    assert "claude setup-token" in err and "claude-quota-a@x" in err
    cooldowns = json.loads((isolated / "cooldowns.json").read_text())
    assert all(set(scopes) == {"*"} for scopes in cooldowns.values())
    until = cooldowns["a@x"]["*"]
    assert delegate.datetime.fromisoformat(until) > delegate._now() + timedelta(days=29)


def test_fable_limit_then_opus_dispatch_reuses_same_lane(
        isolated, monkeypatch):
    lane = capacity_row("claude", "a@x", score=90)
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(claude_rows=[lane]),
    )
    calls = []
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        fake_run_factory(
            [(4, "", "usage limit"), (0, "opus succeeded", "")], calls
        ),
    )

    assert delegate.main([
        "-m", "fable", "fable task", "-o", str(isolated / "fable.md")
    ]) == 3
    cooldowns = json.loads((isolated / "cooldowns.json").read_text())
    assert set(cooldowns["a@x"]) == {"claude-fable-5-1"}

    assert delegate.main([
        "-m", "opus", "opus task", "-o", str(isolated / "opus.md")
    ]) == 0
    assert [cmd[cmd.index("-a") + 1] for cmd in calls] == ["a@x", "a@x"]
    assert [cmd[cmd.index("-m") + 1] for cmd in calls] == [
        "claude-fable-5-1",
        "claude-opus-5",
    ]


def test_rc5_account_cooldown_blocks_fable_and_opus(
        isolated, monkeypatch):
    lane = capacity_row("claude", "a@x", score=90)
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(claude_rows=[lane]),
    )
    calls = []
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        fake_run_factory([(5, "", "organization disabled")], calls),
    )

    assert delegate.main([
        "-m", "fable", "first task", "-o", str(isolated / "first.md")
    ]) == 3
    cooldowns = json.loads((isolated / "cooldowns.json").read_text())
    assert set(cooldowns["a@x"]) == {"*"}

    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("account-cooled lane was dispatched"),
    )
    assert delegate.main([
        "-m", "fable", "second task", "-o", str(isolated / "second.md")
    ]) == 3
    assert delegate.main([
        "-m", "opus", "third task", "-o", str(isolated / "third.md")
    ]) == 3
    assert len(calls) == 1


def test_sync_repick_is_model_scoped_and_other_model_can_reuse_lane(
        isolated, monkeypatch):
    lanes = [
        capacity_row("claude", "a@x", score=90),
        capacity_row("claude", "b@x", score=80),
    ]
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(claude_rows=lanes),
    )
    calls = []
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        fake_run_factory(
            [
                (4, "", "usage limit"),
                (0, "fable succeeded", ""),
                (0, "opus succeeded", ""),
            ],
            calls,
        ),
    )

    assert delegate.main([
        "-m", "fable", "fable task", "-o", str(isolated / "fable.md")
    ]) == 0
    assert delegate.main([
        "-m", "opus", "opus task", "-o", str(isolated / "opus.md")
    ]) == 0

    assert [cmd[cmd.index("-a") + 1] for cmd in calls] == [
        "a@x",
        "b@x",
        "a@x",
    ]
    assert [cmd[cmd.index("-m") + 1] for cmd in calls] == [
        "claude-fable-5-1",
        "claude-fable-5-1",
        "claude-opus-5",
    ]
    cooldowns = json.loads((isolated / "cooldowns.json").read_text())
    assert set(cooldowns["a@x"]) == {"claude-fable-5-1"}


def test_exhausted_codex_automatically_overflows_to_claude(isolated, monkeypatch, capsys):
    calls = []
    exhausted = [capacity_row("codex", "/home/full", dispatchable=False)]
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(codex_rows=exhausted)
    )
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    out = isolated / "out"
    assert delegate.main(["--why", "For each file, check imports", "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "CROSS-FAMILY" in err and "Codex fleet" in err
    assert len(calls) == 1 and "subfleet-claude" in calls[0][0]
    assert calls[0][calls[0].index("-m") + 1] == "claude-opus-5"
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["requested_family"] == "codex" and decision["family"] == "claude"
    assert decision["capacity"]["cache"]["ttl_seconds"] == 120
    assert decision["family_scores"] == {"claude": None, "codex": 0.0}


def test_exhausted_codex_with_credit_uses_policy_picker_before_overflow(
    isolated, monkeypatch, capsys
):
    limited = capacity_row("codex", "/home/full", dispatchable=False)
    limited["reset_credits"] = {"available": 1, "applicable": 1}
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(codex_rows=[limited])
    )
    monkeypatch.setattr(
        delegate,
        "_codex_home",
        lambda: (
            "/home/redeemed",
            "subfleet: redeemed reset on lane@x (0 credits remain fleet-wide); "
            "weekly reset now 2026-08-29T12:00:00Z\n",
        ),
    )
    calls = []
    monkeypatch.setattr(
        delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls)
    )

    assert delegate.main(["For each file, check imports", "-o", str(isolated / "out")]) == 0
    assert calls[0][calls[0].index("-H") + 1] == "/home/redeemed"
    err = capsys.readouterr().err
    assert "redeemed reset on lane@x" in err
    assert "CROSS-FAMILY" not in err


def test_non_hard_sweep_still_overflows_to_claude(isolated, monkeypatch, capsys):
    exhausted = [capacity_row("codex", "/home/full", dispatchable=False)]
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(codex_rows=exhausted)
    )
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["For each file, check imports", "-o", str(isolated / "out")]) == 0
    assert "CROSS-FAMILY" in capsys.readouterr().err
    assert calls[0][calls[0].index("-s") + 1] == "read-only"


@pytest.mark.parametrize(
    ("prompt", "sandbox"),
    [("Review this implementation", "read-only"), ("Implement the endpoint", "workspace-write")],
)
def test_legacy_review_and_build_start_on_opus(isolated, monkeypatch, capsys, prompt, sandbox):
    """Tier-less legacy classes are standard work: Opus on a Claude lane, with
    the class's sandbox default, and no Codex involvement at all."""
    exhausted = [capacity_row("codex", "/home/full", dispatchable=False)]
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(codex_rows=exhausted)
    )
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main([prompt, "-o", str(isolated / "out")]) == 0
    assert "CROSS-FAMILY" not in capsys.readouterr().err
    command = calls[0]
    assert command[0].endswith("subfleet-claude")
    assert command[command.index("-m") + 1] == "claude-opus-5"
    assert command[command.index("-s") + 1] == sandbox
    assert "-e" not in command


def test_legacy_build_moves_upward_to_astra_only_when_opus_is_exhausted(
        isolated, monkeypatch, capsys):
    lane = capacity_row(
        "claude", "lane@x", score=90, scoped_limits=[model_limit("Opus")],
    )
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(claude_rows=[lane])
    )
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["implement x", "-o", str(isolated / "out")]) == 0
    err = capsys.readouterr().err
    assert "CAPABILITY FALLBACK" in err and "build/standard upward to astra" in err
    command = calls[0]
    assert command[0].endswith("subfleet-codex")
    assert command[command.index("-m") + 1] == "gpt-6-astra"
    assert command[command.index("-e") + 1] == "ultra"


def test_all_claude_limited_fable_fails_fast_with_earliest_reset(
        isolated, monkeypatch, capsys):
    early = "2026-07-22T14:00:00-04:00"
    later = "2026-07-22T18:00:00-04:00"
    limited = [
        capacity_row("claude", "a@x", dispatchable=False, limited_until=later),
        capacity_row("claude", "b@x", dispatchable=False, limited_until=early),
    ]
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(claude_rows=limited)
    )
    monkeypatch.setattr(
        delegate.subprocess, "run", lambda *args, **kwargs: pytest.fail("floor was downgraded")
    )
    assert delegate.main(["--why", "Send the final email", "-o", str(isolated / "out")]) == 3
    err = capsys.readouterr().err
    assert "FABLE FLOOR" in err and early in err and "refusing cross-family" in err
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["cmd"] == [] and decision["family_scores"]["claude"] == 0.0


def test_fable_lanes_in_cooldown_only_name_the_cooldown_and_its_reset(
        isolated, monkeypatch, capsys):
    """Four lanes read OK on the table yet every one is shut for Fable by a 5h
    cooldown with no scoped limit on record (2026-09-03): the refusal must say
    cooldown, not 'exhausted scoped limits', and name the earliest reopening."""
    early = (delegate._now() + timedelta(minutes=30)).isoformat()
    later = (delegate._now() + timedelta(hours=2)).isoformat()
    healthy_overall = [
        capacity_row("claude", "a@x", score=90),
        capacity_row("claude", "b@x", score=80),
    ]
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(claude_rows=healthy_overall),
    )
    cooldowns = delegate._load_cooldowns()
    cooldowns.setdefault("a@x", {})["claude-fable-5-1"] = later
    cooldowns.setdefault("b@x", {})["claude-fable-5-1"] = early
    delegate._save_cooldowns(cooldowns)
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("cooled Fable floor was dispatched"),
    )

    assert delegate.main([
        "--why", "Send the final email", "-o", str(isolated / "out")
    ]) == 3

    err = capsys.readouterr().err
    assert "FABLE FLOOR" in err
    assert "in a Fable cooldown" in err
    assert "exhausted Fable scoped limits" not in err
    assert "earliest reset" in err
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    blocked = {item["lane"]: item for item in decision["scoped_limit_reasoning"]["blocked_lanes"]}
    assert set(blocked) == {"a@x", "b@x"}
    assert all(item["limit"] is None for item in blocked.values())
    assert all(item["cooldown_until"] for item in blocked.values())


def test_all_fable_scopes_exhausted_fails_fast_with_scoped_reset_and_reasoning(
        isolated, monkeypatch, capsys):
    early = "2026-07-28T04:59:59Z"
    later = "2026-07-29T04:59:59Z"
    healthy_overall = [
        capacity_row(
            "claude", "a@x", score=90, scoped_limits=[fable_limit(later)]
        ),
        capacity_row(
            "claude", "b@x", score=80, scoped_limits=[fable_limit(early)]
        ),
    ]
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(claude_rows=healthy_overall),
    )
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("exhausted Fable floor was dispatched"),
    )

    assert delegate.main([
        "--why", "Send the final email", "-o", str(isolated / "out")
    ]) == 3

    err = capsys.readouterr().err
    assert "FABLE FLOOR" in err
    assert "exhausted Fable scoped limits" in err
    assert f"earliest reset {early}" in err
    assert "refusing cross-family downgrade" in err
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    reasoning = decision["scoped_limit_reasoning"]
    assert decision["cmd"] == []
    assert decision["family_scores"]["claude"] == 90
    assert reasoning["model_family"] == "Fable"
    assert reasoning["eligible_lanes"] == []
    assert {item["lane"] for item in reasoning["blocked_lanes"]} == {"a@x", "b@x"}
    assert reasoning["selected_lane"] is None


@pytest.mark.parametrize(
    ("prompt", "sandbox"),
    [
        ("Implement the endpoint", "workspace-write"),
        ("Review this patch", "read-only"),
    ],
)
def test_fable_scope_does_not_block_opus_build_or_review(
        isolated, monkeypatch, capsys, prompt, sandbox):
    lanes = [
        capacity_row(
            "claude",
            "fable-blocked@x",
            score=90,
            scoped_limits=[fable_limit("2026-07-28T04:59:59Z")],
        )
    ]
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(claude_rows=lanes)
    )
    calls = []
    monkeypatch.setattr(
        delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls)
    )

    assert delegate.main([
        "-m", "opus", "--why", prompt, "-o", str(isolated / "out")
    ]) == 0

    assert calls[0][calls[0].index("-a") + 1] == "fable-blocked@x"
    assert calls[0][calls[0].index("-m") + 1] == "claude-opus-5"
    assert calls[0][calls[0].index("-s") + 1] == sandbox
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["scoped_limit_reasoning"]["model_family"] == "Opus"
    assert decision["scoped_limit_reasoning"]["blocked_lanes"] == []
    assert decision["scoped_limit_reasoning"]["selected_lane"] == "fable-blocked@x"


def test_unknown_codex_telemetry_does_not_trigger_cross_family_overflow(
        isolated, monkeypatch, capsys):
    unknown = [
        capacity_row(
            "codex", "/home/unknown", dispatchable=False, status="network-error"
        )
    ]
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(codex_rows=unknown)
    )
    monkeypatch.setattr(
        delegate.subprocess, "run", lambda *args, **kwargs: pytest.fail("unknown lane dispatched")
    )
    assert delegate.main(["For each file, check imports", "-o", str(isolated / "out")]) == 3
    assert "CROSS-FAMILY" not in capsys.readouterr().err


def test_explicit_home_pin_bypasses_capacity_rerouting(isolated, monkeypatch, capsys):
    exhausted = [capacity_row("codex", "/home/full", dispatchable=False)]
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(codex_rows=exhausted)
    )
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["-H", "/pinned", "implement x", "-o", str(isolated / "out")]) == 0
    assert calls[0][calls[0].index("-H") + 1] == "/pinned"
    assert "CROSS-FAMILY" not in capsys.readouterr().err


def test_explicit_model_and_lane_pins_are_not_capacity_overridden(
        isolated, monkeypatch, capsys):
    exhausted_codex = [capacity_row("codex", "/home/full", dispatchable=False)]
    limited_claude = [capacity_row("claude", "full@x", dispatchable=False)]
    monkeypatch.setattr(
        delegate,
        "_capacity_report",
        lambda: capacity_snapshot(codex_rows=exhausted_codex, claude_rows=limited_claude),
    )
    assert delegate.main(["-m", "sol", "implement x", "-o", str(isolated / "out")]) == 3
    assert "CROSS-FAMILY" not in capsys.readouterr().err

    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main([
        "-m", "fable", "-a", "pinned@x", "task", "-o", str(isolated / "out")
    ]) == 0
    assert calls[0][calls[0].index("-a") + 1] == "pinned@x"


def test_preamble_defaults_off_dry_run_and_decision(isolated, monkeypatch, capsys):
    seen = []
    def run(cmd, **kwargs):
        if cmd[-1:] == ["--json"] or "status" in cmd:
            return CompletedProcess(cmd, 1, "", "")
        if "pick" in cmd and "codex" in cmd:
            return CompletedProcess(cmd, 0, "/home/codex\n", "")
        seen.append((cmd, Path(cmd[cmd.index("-p") + 1]).read_text()))
        return CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(delegate.subprocess, "run", run)
    out = isolated / "out"
    assert delegate.main(["implement x", "-o", str(out)]) == 0
    assert "Standing orders" in seen[0][1] and seen[0][0][seen[0][0].index("-s") + 1] == "workspace-write"
    assert seen[0][0][seen[0][0].index("-m") + 1] == "claude-opus-5" and "-e" not in seen[0][0]
    seen.clear()
    assert delegate.main(["--task", "build", "--tier", "hard", "implement x", "-o", str(out)]) == 0
    assert seen[0][0][seen[0][0].index("-m") + 1] == "gpt-6-astra"
    assert "-e" in seen[0][0] and "ultra" in seen[0][0]
    seen.clear()
    assert delegate.main(["review x", "-o", str(out)]) == 0
    assert delegate.PREAMBLE_AUDIT in seen[0][1]
    assert "Standing orders" not in seen[0][1]
    assert seen[0][0][seen[0][0].index("-s") + 1] == "read-only"
    seen.clear()
    assert delegate.main(["Send this email as Max", "-o", str(out)]) == 0
    assert seen[0][1] == "Send this email as Max"
    assert seen[0][0][seen[0][0].index("-s") + 1] == "read-only"
    seen.clear()
    assert delegate.main(["-m", "fable", "--no-preamble", "review x", "-o", str(out)]) == 0
    assert seen[0][1] == "review x" and seen[0][0][seen[0][0].index("-s") + 1] == "read-only"
    seen.clear()
    assert delegate.main(["--dry-run", "-H", "/h", "implement x", "-o", str(out)]) == 0
    assert not seen and "subfleet-codex" in capsys.readouterr().out
    assert len((isolated / "decisions.jsonl").read_text().splitlines()) == 6


def test_detach_without_output_hosts_it_in_the_ledger(isolated, tmp_path, monkeypatch, capsys):
    """`-d` no longer needs -o: the run directory hosts out.md, err and lane logs."""
    runner = tmp_path / "fake-subfleet-codex"
    runner.write_text(
        """#!/bin/bash
out=''
while [ "$#" -gt 0 ]; do
  case "$1" in -o) out=$2; shift 2 ;; *) shift ;; esac
done
printf 'hosted answer\\n' > "$out"
"$SUBFLEET_TEST_BIN" _record-run --phase finish --run-id "$SUBFLEET_RUN_ID" --rc 0 >/dev/null 2>&1
rm -f "${SUBFLEET_RUN_OWNED_PROMPT:-}"
"""
    )
    runner.chmod(0o755)
    ledger_state = tmp_path / "ledger-state"
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(ledger_state))
    monkeypatch.setenv("DELEGATE_CODEX_RUN", str(runner))
    monkeypatch.setenv("SUBFLEET_TEST_BIN", str(Path(__file__).parent.parent / "bin" / "subfleet"))

    assert delegate.main(["-d", "-H", "/home/codex", "-C", str(tmp_path), "-n", "hosted", "task"]) == 0
    banner = capsys.readouterr().out
    assert "subfleet run: dispatched run=" in banner
    run_id = banner.split("run=", 1)[1].split()[0]
    assert run_id.endswith("-hosted")
    run_dir = ledger_state / "runs" / run_id
    assert f"out: {run_dir / 'out.md'}" in banner
    assert f"log: {run_dir / 'out.lane.log'}" in banner

    deadline = time.monotonic() + 20  # generous: suites run under heavy lane load
    while time.monotonic() < deadline and json.loads((run_dir / "meta.json").read_text()).get("finished_at") is None:
        time.sleep(0.05)
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["finished_at"] is not None and meta["rc"] == 0
    assert meta["launcher"] == "subfleet run"
    assert meta["original_out_path"] is None
    assert (run_dir / "out.md").read_text() == "hosted answer\n"


def test_in_session_default_is_detached_and_attach_waits(isolated, tmp_path, monkeypatch, capsys):
    """Inside a Claude session (CLAUDECODE=1) `subfleet run` detaches by default;
    --attach keeps the detached launch but blocks until the ledger says done."""
    runner = tmp_path / "fake-subfleet-codex"
    runner.write_text(
        """#!/bin/bash
out=''
while [ "$#" -gt 0 ]; do
  case "$1" in -o) out=$2; shift 2 ;; *) shift ;; esac
done
sleep 0.3
printf 'attached answer\\n' > "$out"
"$SUBFLEET_TEST_BIN" _record-run --phase finish --run-id "$SUBFLEET_RUN_ID" --rc 0 >/dev/null 2>&1
rm -f "${SUBFLEET_RUN_OWNED_PROMPT:-}"
"""
    )
    runner.chmod(0o755)
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(tmp_path / "ledger-state"))
    monkeypatch.setenv("DELEGATE_CODEX_RUN", str(runner))
    monkeypatch.setenv("SUBFLEET_TEST_BIN", str(Path(__file__).parent.parent / "bin" / "subfleet"))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "11111111-2222-4333-8444-555555555555")

    started = time.monotonic()
    assert delegate.main(["-H", "/home/codex", "-C", str(tmp_path), "-o", str(tmp_path / "a.md"), "task"]) == 0
    assert time.monotonic() - started < 0.25
    out = capsys.readouterr().out
    assert "detached — inside a Claude session" in out
    assert "gets a completion message" in out or "subfleet wait" in out

    started = time.monotonic()
    assert delegate.main(["--attach", "-H", "/home/codex", "-C", str(tmp_path), "task two"]) == 0
    elapsed = time.monotonic() - started
    assert elapsed >= 0.3
    captured = capsys.readouterr()
    assert "waiting inline" in captured.out
    assert "attached answer" in captured.out  # no -o → output echoed, as in sync mode
    assert "subfleet run: " in captured.err and "FINISHED" in captured.err

    runs = json.loads((tmp_path / "ledger-state" / "runs").glob("*/meta.json").__next__().read_text())
    assert runs["caller"]["session_id"] == "11111111-2222-4333-8444-555555555555"


def test_outside_session_stays_synchronous(isolated, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["-m", "fable", "-o", str(tmp_path / "o.md"), "task"]) == 0
    assert calls and calls[0][0].endswith("subfleet-claude")
    assert "-A" not in calls[0]
    assert "dispatched run=" not in capsys.readouterr().out


def test_env_override_forces_sync_inside_session(isolated, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("SUBFLEET_RUN_DETACH", "0")
    assert delegate.main(["-m", "fable", "-o", str(tmp_path / "o.md"), "task"]) == 0
    assert calls and calls[0][0].endswith("subfleet-claude")


def test_codex_detach_returns_before_runner_finishes(
        isolated, tmp_path, monkeypatch, capsys):
    runner = tmp_path / "fake-subfleet-codex"
    runner.write_text(
        """#!/bin/bash
prompt=''
out=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p) prompt=$2; shift 2 ;;
    -o) out=$2; shift 2 ;;
    *) shift ;;
  esac
done
i=0
while [ ! -e "$FAKE_RELEASE" ] && [ "$i" -lt 40 ]; do
  sleep 0.05
  i=$((i + 1))
done
cat "$prompt" > "$out"
rm -f "${SUBFLEET_RUN_OWNED_PROMPT:-}"
printf 'fake runner finished\n'
"""
    )
    runner.chmod(0o755)
    release = tmp_path / "release"
    output = tmp_path / "answer.md"
    lane_log = tmp_path / "answer.lane.log"
    monkeypatch.setenv("DELEGATE_CODEX_RUN", str(runner))
    monkeypatch.setenv("FAKE_RELEASE", str(release))

    started = time.monotonic()
    result = delegate.main([
        "-d", "-H", "/home/codex", "-C", str(tmp_path),
        "-o", str(output), "implement x",
    ])
    elapsed = time.monotonic() - started
    release.touch()

    assert result == 0
    assert elapsed < 1
    message = capsys.readouterr().out
    first = message.splitlines()[0]
    assert first.startswith("subfleet run: dispatched run=")
    assert "model=astra" in first and "lane=/home/codex" in first and "(detached — -d)" in first
    pid = int(first.split("pid=", 1)[1].split()[0])
    assert pid > 0
    assert f"  out: {output}" in message
    assert f"  log: {lane_log}" in message
    assert "subfleet wait " in message

    deadline = time.monotonic() + 20  # generous: suites run under heavy lane load
    while time.monotonic() < deadline and (
        not output.exists()
        or "implement x" not in output.read_text()
        or not lane_log.exists()
        or "fake runner finished" not in lane_log.read_text()
    ):
        time.sleep(0.05)
    prompt_text = output.read_text()
    assert delegate.PREAMBLE_WRITE in prompt_text
    assert prompt_text.endswith("implement x")
    assert "fake runner finished" in lane_log.read_text()
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["result"] == 0
    assert decision["cmd"][0] == str(runner)
    assert not Path(decision["cmd"][decision["cmd"].index("-p") + 1]).exists()


def test_detached_runner_owns_prompt_after_delegate_cleanup(
        isolated, tmp_path, monkeypatch):
    from test_claude_lane_script import SCRIPT as CLAUDE_LANE, _fixture_env

    lane_root = tmp_path / "lane-fixture"
    lane_root.mkdir()
    runner_env, run_paths = _fixture_env(
        lane_root,
        """payload=$(cat)
case "$payload" in
  *"Send final email"*) result='detached child saw prompt' ;;
  *) result='detached child missed prompt' ;;
esac
printf '{"is_error":false,"result":"%s"}\n' "$result"
""",
    )
    for key in (
        "PATH", "CLAUDE_LANE_CLAUDE", "CLAUDE_LANE_AGENT_SECRET",
        "CLAUDE_LANE_SUBFLEET", "CLAUDE_LANE_CLAUDE_MODEL",
        "CLAUDE_LANE_BACKOFF", "HOOK_LOG", "HOOK_EXIT", "UUID_COUNTER",
    ):
        monkeypatch.setenv(key, runner_env[key])
    monkeypatch.setenv("CLAUDE_LANE_DETACHED_START_DELAY", "0.25")
    private_tmp = tmp_path / "delegate-private-prompts"
    private_tmp.mkdir()
    monkeypatch.setenv("CLAUDE_LANE_TMPDIR", str(private_tmp))
    monkeypatch.setenv("DELEGATE_CLAUDE_LANE", str(CLAUDE_LANE))
    output = tmp_path / "delegate-answer.md"

    assert delegate.main([
        "-d", "-C", str(run_paths["workdir"]), "-o", str(output),
        "Send final email",
    ]) == 0

    deadline = time.monotonic() + 20  # generous: suites run under heavy lane load
    while time.monotonic() < deadline and (
        not output.exists()
        or output.read_text().strip() != "detached child saw prompt"
        or list(private_tmp.glob("subfleet-claude-prompt.*"))
    ):
        time.sleep(0.05)
    assert output.read_text().strip() == "detached child saw prompt"
    assert list(private_tmp.glob("subfleet-claude-prompt.*")) == []


@pytest.mark.parametrize("detached", [False, True])
def test_real_codex_runner_ledgers_delegate_decision_and_detached_log(
    isolated, tmp_path, monkeypatch, detached
):
    fake_bin = tmp_path / "delegate-bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        """#!/bin/bash
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then out=$2; shift 2; else shift; fi
done
printf 'delegated result\n' > "$out"
"""
    )
    fake_codex.chmod(0o755)
    runner = Path(__file__).parent.parent / "bin" / "subfleet-codex"
    ledger_state = tmp_path / "ledger-state"
    workdir = tmp_path / "delegated-work"
    workdir.mkdir()
    home = tmp_path / "codex-lane"
    home.mkdir()
    output = tmp_path / "delegated-answer.md"
    row = capacity_row("codex", str(home), score=90)
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(codex_rows=[row])
    )
    monkeypatch.setenv("DELEGATE_CODEX_RUN", str(runner))
    monkeypatch.setenv("SUBFLEET_CODEX_GUARD", "off")
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(ledger_state))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    argv = [
        "-m", "sol", "-C", str(workdir), "-o", str(output),
        "implement delegated ledger",
    ]
    if detached:
        argv.insert(0, "-d")
    assert delegate.main(argv) == 0

    deadline = time.monotonic() + 8
    run_dir = None
    meta = None
    while time.monotonic() < deadline:
        candidates = list((ledger_state / "runs").glob("*/meta.json"))
        if candidates:
            candidate_meta = json.loads(candidates[0].read_text())
            if candidate_meta.get("finished_at") is not None:
                run_dir = candidates[0].parent
                meta = candidate_meta
                break
        time.sleep(0.05)

    assert run_dir is not None and meta is not None
    assert meta["rc"] == 0
    assert meta["original_out_path"] == str(output)
    assert meta["routing_decision"]["family"] == "codex"
    assert meta["routing_decision"]["lane/home"] == str(home)
    assert meta["routing_decision"]["result"] == 0
    assert meta["routing_decision"]["cmd"][0] == str(runner)
    saved_prompt = (run_dir / "prompt.md").read_text()
    assert delegate.PREAMBLE_WRITE in saved_prompt
    assert saved_prompt.endswith("implement delegated ledger")
    assert (run_dir / "out.md").read_text() == "delegated result\n"
    if detached:
        assert "subfleet codex: OK" in (run_dir / "lane.log").read_text()


def test_opus_requests_drain_fable_stranded_lanes_first():
    """Max 8/26: an opus dispatch prefers a lane whose fable is exhausted —
    even one with LESS opus headroom — over a fable-capable lane, so opus burn
    lands on capacity fable can no longer use."""
    stranded = capacity_row(
        "claude",
        "stranded@x",
        score=60,
        scoped_limits=[fable_limit("2026-08-30T12:00:00Z")],
    )
    fable_capable = capacity_row("claude", "fable-capable@x", score=95)
    data = {"accounts": [stranded, fable_capable]}

    opus_order = [
        row["id"] for row in delegate._capacity_candidates(
            data, "claude", model_family="Opus"
        )
    ]
    assert opus_order == ["stranded@x", "fable-capable@x"]
    # a fable request is untouched by the preference: only the capable lane serves
    assert [
        row["id"] for row in delegate._capacity_candidates(
            data, "claude", model_family="Fable"
        )
    ] == ["fable-capable@x"]


# ---------------------------------------------------------------------------
# 2026-09-03: blind lanes (no usage reading) must be probed before dispatch,
# and a lane that stays blind may not stack runs. Five Opus lanes died mid-run
# that day because the picker treated "score None" as dispatchable.


def _blind(email, in_flight=0):
    return capacity_row("claude", email, score=None, confidence="estimated",
                        in_flight=in_flight)


def test_blind_lane_measured_over_floor_is_kept_and_ranked_first(monkeypatch):
    monkeypatch.setattr(delegate, "_probe_lane_headroom",
                        lambda email: {"score": 60.0, "reset_at": None, "status": "ok"})
    monkeypatch.setattr(delegate.capacity, "store_lane_cooldown",
                        lambda *a, **k: pytest.fail("no cooldown expected"))
    blind = _blind("blind@x", in_flight=3)
    known = capacity_row("claude", "known@x", score=20)
    rows = delegate._capacity_candidates(
        {"accounts": [known, blind]}, "claude", model_family="Opus")
    assert [r["id"] for r in rows] == ["blind@x", "known@x"]
    assert rows[0]["headroom_score"] == 60.0 and rows[0]["jit_probe"] == "ok"


def test_blind_lane_measured_capped_is_dropped_and_cooled(monkeypatch):
    monkeypatch.setattr(delegate, "_probe_lane_headroom",
                        lambda email: {"score": 0.0, "reset_at": "2026-09-03T20:43:00-04:00",
                                       "status": "ok"})
    cooled = {}
    monkeypatch.setattr(delegate.capacity, "store_lane_cooldown",
                        lambda email, until, model=None: cooled.update({email: (until, model)}))
    rows = delegate._capacity_candidates(
        {"accounts": [_blind("capped@x")]}, "claude", model_family="Opus")
    assert rows == []
    assert "capped@x" in cooled and cooled["capped@x"][0].isoformat().startswith("2026-09-03T20:43")


def test_run_exclude_flag_parses_and_removes_accounts_from_the_pick(monkeypatch):
    """`subfleet run --exclude EMAIL` (repeatable): the picker must never place a
    reconcile on an account that ran one of the pair's lanes (2026-09-04: it
    did, silently breaking blind independence, and had to be killed)."""
    args = delegate._parser().parse_args(
        ["-m", "opus", "--exclude", "a@x", "-x", "b@x", "-C", ".", "-p", "brief.md"])
    assert args.exclude == ["a@x", "b@x"]
    monkeypatch.setattr(delegate, "_probe_lane_headroom",
                        lambda email: {"score": None, "reset_at": None, "status": "http-403"})
    rows = [capacity_row("claude", e, score=None, confidence="estimated") for e in ("a@x", "b@x", "c@x")]
    picked = delegate._capacity_candidates(
        {"accounts": rows}, "claude", set(args.exclude), model_family="Opus")
    assert [r["id"] for r in picked] == ["c@x"]


def test_active_desktop_login_ranks_last_among_dispatchable_lanes(monkeypatch):
    """2026-09-04: with every lane token blind, the desktop login was the only
    MEASURED lane and therefore ranked "best" despite the handicap — the one
    account Max's own sessions need. It is the last resort: every other
    dispatchable lane, measured or blind, ranks ahead of it; alone, it is
    still picked."""
    monkeypatch.setattr(delegate, "_probe_lane_headroom",
                        lambda email: {"score": None, "reset_at": None, "status": "http-403"})
    login = capacity_row("claude", "login@x", score=90)
    login["active"] = True
    measured = capacity_row("claude", "known@x", score=20)
    blind = _blind("blind@x", in_flight=0)
    rows = delegate._capacity_candidates(
        {"accounts": [login, measured, blind]}, "claude", model_family="Opus")
    assert [r["id"] for r in rows] == ["known@x", "blind@x", "login@x"]
    alone = delegate._capacity_candidates(
        {"accounts": [login]}, "claude", model_family="Opus")
    assert [r["id"] for r in alone] == ["login@x"]


def test_lane_that_stays_blind_may_not_stack_runs(monkeypatch):
    monkeypatch.setattr(delegate, "_probe_lane_headroom",
                        lambda email: {"score": None, "reset_at": None, "status": "token-invalid"})
    idle = _blind("idle@x", in_flight=0)
    busy = _blind("busy@x", in_flight=1)
    rows = delegate._capacity_candidates(
        {"accounts": [busy, idle]}, "claude", model_family="Opus")
    assert [r["id"] for r in rows] == ["idle@x"]
    assert rows[0]["jit_probe"] == "token-invalid"


def test_measured_lanes_are_not_probed(monkeypatch):
    monkeypatch.setattr(delegate, "_probe_lane_headroom",
                        lambda email: pytest.fail(f"probe called for {email}"))
    known = capacity_row("claude", "known@x", score=50, in_flight=4)
    rows = delegate._capacity_candidates(
        {"accounts": [known]}, "claude", model_family="Opus")
    assert [r["id"] for r in rows] == ["known@x"]


def test_jit_probe_cache_and_score_derivation(monkeypatch):
    calls = []

    def fake_probe(token, timeout=10.0):
        calls.append(token)
        return {"status": "ok", "five_hour": {"used_percent": 30, "reset_at": "r5"},
                "seven_day": {"used_percent": 80, "reset_at": "r7"}}

    monkeypatch.setattr(delegate.claude_side, "probe_oauth_usage", fake_probe)
    monkeypatch.setattr(delegate.claude_side, "roster_config",
                        lambda: {"enrolled": {"p@x": "claude-quota-p@x"}})
    monkeypatch.setattr(delegate.claude_side, "agent_secret_get",
                        lambda name, runner=None: "tok")
    delegate._jit_probe_cache.clear()
    first = delegate._probe_lane_headroom("p@x")
    second = delegate._probe_lane_headroom("p@x")
    assert first["score"] == 20.0 and first["reset_at"] == "r5"
    assert second is first and calls == ["tok"]


def test_astra_alias_is_gpt6_on_a_codex_lane_at_ultra_effort(isolated, monkeypatch):
    assert delegate.MODEL_FAMILY["astra"] == "codex"
    assert delegate.MODEL_NAMES["astra"] == "gpt-6-astra"
    assert "astra" in delegate.CODEX_ULTRA_EFFORT_MODELS
    assert delegate.choose_model("build", "astra") == "astra"
    seen = []

    def run(cmd, **kwargs):
        if cmd[-1:] == ["--json"] or "status" in cmd:
            return CompletedProcess(cmd, 1, "", "")
        if "pick" in cmd and "codex" in cmd:
            return CompletedProcess(cmd, 0, "/home/codex\n", "")
        seen.append(cmd)
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(delegate.subprocess, "run", run)
    out = isolated / "out"
    assert delegate.main(["-m", "astra", "implement x", "-o", str(out)]) == 0
    command = seen[0]
    assert command[command.index("-m") + 1] == "gpt-6-astra"
    assert command[command.index("-e") + 1] == "ultra"
    assert "-A" in command  # auto-picked lane: the runner may re-pick on a limit
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["overrides"]["model"] == "astra"


def test_terra_is_not_forced_to_ultra_effort(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["-m", "terra", "implement x", "-o", str(isolated / "out")]) == 0
    assert "-e" not in calls[0]


def test_api_key_home_pin_is_refused_unless_deliberately_overridden(
        isolated, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("SUBFLEET_ALLOW_API_LANE", raising=False)
    api_home = tmp_path / "codex-api"
    api_home.mkdir()
    (api_home / "auth.json").write_text(json.dumps({
        "OPENAI_API_KEY": "sk-test", "auth_mode": "apikey", "tokens": None,
    }))
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    with pytest.raises(SystemExit) as refused:
        delegate.main(["-H", str(api_home), "-m", "astra", "implement x", "-o", str(isolated / "out")])
    assert refused.value.code == 2
    assert "ChatGPT subscriptions only" in capsys.readouterr().err
    assert calls == []

    monkeypatch.setenv("SUBFLEET_ALLOW_API_LANE", "1")
    assert delegate.main([
        "-H", str(api_home), "-m", "astra", "implement x", "-o", str(isolated / "out"),
    ]) == 0
    assert calls[0][calls[0].index("-H") + 1] == str(api_home)


def test_chatgpt_home_pin_passes_the_api_guard(isolated, monkeypatch, tmp_path):
    monkeypatch.delenv("SUBFLEET_ALLOW_API_LANE", raising=False)
    lane = tmp_path / "codex-4"
    lane.mkdir()
    (lane / "auth.json").write_text(json.dumps({
        "OPENAI_API_KEY": None, "auth_mode": "chatgpt",
        "tokens": {"access_token": "x", "account_id": "acct"},
    }))
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["-H", str(lane), "-m", "astra", "implement x", "-o", str(isolated / "out")]) == 0
    assert calls[0][calls[0].index("-H") + 1] == str(lane)


def test_lane_pin_rejects_non_codex_models_with_all_aliases_named(isolated, capsys):
    with pytest.raises(SystemExit):
        delegate.main(["-H", "/pinned", "-m", "opus", "task", "-o", str(isolated / "out")])
    assert "sol, terra, astra" in capsys.readouterr().err


def test_retired_sol_alias_dispatches_astra_and_says_so(isolated, monkeypatch, capsys):
    assert delegate.RETIRED_MODEL_ALIASES == {"sol": "astra"}
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["-m", "sol", "implement x", "-o", str(isolated / "out")]) == 0
    command = calls[0]
    assert command[command.index("-m") + 1] == "gpt-6-astra"
    assert command[command.index("-e") + 1] == "ultra"
    err = capsys.readouterr().err
    assert "sol is retired from dispatch" in err and "dispatching astra instead" in err
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["overrides"]["model"] == "sol"  # what the caller asked for
    assert decision["model"] == "astra"  # what ran


def test_no_automatic_route_selects_sol():
    for task, tiers in delegate.SEMANTIC_MODEL_GRID.items():
        for tier, model in tiers.items():
            assert model != "sol", (task, tier)
    for tier, chain in delegate.UPWARD_MODEL_CHAINS.items():
        assert "sol" not in chain, tier
    for task_class in ("fable", "review", "sweep", "build"):
        assert delegate.choose_model(task_class) != "sol"


def test_run_refuses_an_output_path_a_live_run_is_still_writing(monkeypatch, capsys):
    """2026-09-04: two runs launched with one -o path (a killed misroute and its
    pinned replacement) nearly wrote one reconcile deliverable twice, and the
    file was then attributed to the wrong run. A live writer refuses the path
    unless --reuse-out; a finished writer is only named."""
    args = delegate._parser().parse_args(["-m", "opus", "-o", "x.md", "--reuse-out", "-p", "b.md"])
    assert args.reuse_out is True
    assert delegate._parser().parse_args(["-m", "opus", "-p", "b.md"]).reuse_out is False
    live = [{"id": "r-live", "lane": "a@x", "pid": 7, "finished_at": None, "live": True}]
    monkeypatch.setattr(delegate.run_ledger, "output_collisions", lambda out: live)
    assert delegate._output_path_guard("x.md") == 3
    assert "r-live" in capsys.readouterr().err
    assert delegate._output_path_guard("x.md", reuse=True) is None
    done = [{"id": "r-done", "lane": "a@x", "pid": 7,
             "finished_at": "2026-09-04T18:39:32-04:00", "live": False}]
    monkeypatch.setattr(delegate.run_ledger, "output_collisions", lambda out: done)
    assert delegate._output_path_guard("x.md") is None
    assert "r-done" in capsys.readouterr().err
    monkeypatch.setattr(delegate.run_ledger, "output_collisions", lambda out: [])
    assert delegate._output_path_guard("x.md") is None
    assert delegate._output_path_guard(None) is None


def test_blind_busy_lanes_are_inconclusive_not_a_model_limit(monkeypatch):
    """2026-09-05 13:2x: every Opus-eligible lane was blind (usage endpoint
    rate-limited) and carried one in-flight run, the blind filter withheld all
    of them, and _model_capacity_state read the empty list as a model-scoped
    Opus limit, escalating two standard dispatches to Astra. Withheld-but-
    eligible lanes are inconclusive telemetry, not exhaustion."""
    monkeypatch.setattr(delegate, "_probe_lane_headroom",
                        lambda email: {"score": None, "reset_at": None, "status": "rate-limited"})
    busy = [_blind(f"busy{i}@x", in_flight=1) for i in range(3)]
    data = {"accounts": busy}
    assert delegate._capacity_candidates(data, "claude", model_family="Opus") == []
    assert delegate._model_capacity_state(data, "opus") is None
    model, states = delegate.select_semantic_model(data, ["opus", "astra"])
    assert model == "opus" and states["opus"] is None


def test_scoped_limit_on_every_lane_is_still_a_model_limit(monkeypatch):
    monkeypatch.setattr(delegate, "_probe_lane_headroom",
                        lambda email: {"score": None, "reset_at": None, "status": "ok"})
    monkeypatch.setattr(delegate.capacity, "dispatchable_for",
                        lambda row, model_family: False)
    rows = [_blind(f"lim{i}@x", in_flight=0) for i in range(2)]
    assert delegate._model_capacity_state({"accounts": rows}, "opus") is False


def test_luna_alias_is_the_codex_route_for_simple_work():
    """Max 2026-09-11: GPT-5.6 Luna takes the simple subagent work, so the
    trivial and easy tiers leave the Claude pool entirely."""
    assert delegate.MODEL_NAMES["luna"] == "gpt-5.6-luna"
    assert delegate.MODEL_FAMILY["luna"] == "codex"
    assert "luna" in delegate.CODEX_MODEL_CHOICES
    assert "luna" not in delegate.CODEX_ULTRA_EFFORT_MODELS  # catalog default "medium"
    assert delegate.choose_model("build", "luna") == "luna"
    for task in ("lookup", "research", "review", "build"):
        for tier in ("trivial", "easy"):
            assert delegate.choose_model(task, tier=tier) == "luna"
            assert delegate.MODEL_FAMILY[delegate.semantic_model_candidates(task, tier)[0]] == "codex"
    assert delegate.choose_model("sweep", tier="easy") == "terra"
    assert delegate.choose_model("build", tier="standard") == "opus"


@pytest.mark.parametrize("tier", ["trivial", "easy"])
def test_luna_tiers_ignore_claude_scoped_limits(isolated, monkeypatch, capsys, tier):
    """A Claude lane limited on Haiku and Sonnet no longer touches the trivial
    and easy tiers: they dispatch gpt-5.6-luna on a Codex home."""
    lane = capacity_row(
        "claude", "lane@x", score=90,
        scoped_limits=[model_limit("Haiku"), model_limit("Sonnet")],
    )
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(claude_rows=[lane])
    )
    calls = []
    monkeypatch.setattr(
        delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls)
    )
    assert delegate.main([
        "--task", "build", "--tier", tier, "implement x",
        "-o", str(isolated / f"luna-{tier}.md"),
    ]) == 0
    assert calls[0][calls[0].index("-m") + 1] == "gpt-5.6-luna"
    assert "-e" not in calls[0]  # catalog default effort, not ultra
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["requested_model"] == "luna" and decision["model"] == "luna"
    assert "CAPABILITY FALLBACK" not in capsys.readouterr().err


@pytest.mark.parametrize("tier", ["trivial", "easy"])
def test_luna_moves_upward_to_opus_when_codex_is_exhausted(
    isolated, monkeypatch, capsys, tier
):
    """With every Codex home limited, simple work moves upward to Opus on a
    Claude lane (never down), and the decision records why."""
    exhausted = [capacity_row("codex", "/home/full", dispatchable=False)]
    monkeypatch.setattr(
        delegate, "_capacity_report", lambda: capacity_snapshot(codex_rows=exhausted)
    )
    calls = []
    monkeypatch.setattr(
        delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls)
    )
    assert delegate.main([
        "--task", "build", "--tier", tier, "implement x",
        "-o", str(isolated / f"luna-up-{tier}.md"),
    ]) == 0
    assert calls[0][calls[0].index("-m") + 1] == "claude-opus-5"
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["requested_model"] == "luna"
    assert decision["model"] == "opus"
    assert decision["routing_capacity_states"]["luna"] is False
    assert "CAPABILITY FALLBACK" in capsys.readouterr().err

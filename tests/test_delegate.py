import json
from datetime import timedelta
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from subfleet import config, delegate


@pytest.mark.parametrize(("prompt", "kind", "model"), [
    ("Match this voice", "fable", "fable"),
    ("Prepare an email", "fable", "fable"),
    ("Create a blog", "fable", "fable"),
    ("Outline an essay", "fable", "fable"),
    ("Polish the prose", "fable", "fable"),
    ("Adjudicating this dispute", "fable", "fable"),
    ("Give the verdict", "fable", "fable"),
    ("Run the final review", "fable", "fable"),
    ("Run the merge gate", "fable", "fable"),
    ("Prepare the launch", "fable", "fable"),
    ("Send this now", "fable", "fable"),
    ("Design the interface", "fable", "fable"),
    ("Choose a strategy", "fable", "fable"),
    ("Wdyt about this?", "fable", "fable"),
    ("Review this patch", "review", "opus"),
    ("Assess this patch", "review", "opus"),
    ("Critique this patch", "review", "opus"),
    ("Audit this patch", "review", "opus"),
    ("Evaluate this patch", "review", "opus"),
    ("Referee this dispute", "review", "opus"),
    ("Final review before we implement the fix", "fable", "fable"),
    ("Audit and implement the fix", "review", "opus"),
    ("For each file, check imports", "sweep", "terra"),
    ("Extract ids across all rows", "sweep", "terra"),
    ("Count a batch of records", "sweep", "terra"),
    ("For each feature implement it", "build", "opus"),
    ("Implement the voicemail redesign launcher", "build", "opus"),
    ("Implement the endpoint", "build", "opus"),
    ("Fix and test the bug", "build", "opus"),
    ("Refactor the parser", "build", "opus"),
])
def test_routing_table(prompt, kind, model):
    got, _ = delegate.classify(prompt)
    assert got == kind
    assert delegate.choose_model(got) == model


def test_overrides_and_haiku_only_explicit():
    kind, _ = delegate.classify("review this", "build")
    assert (kind, delegate.choose_model(kind, "terra")) == ("build", "terra")
    assert delegate.choose_model("fable") != "haiku"
    assert delegate.choose_model("build", "haiku") == "haiku"
    assert delegate.choose_model("review") == "opus"
    assert delegate.choose_model("build") == "opus"
    assert delegate.choose_model("build", "opus") == "opus"
    assert delegate.choose_model("build", "astra") == "astra"
    assert delegate.choose_model("build", "sol") == "astra"  # retired alias
    assert delegate.MODEL_NAMES["opus"] == "claude-opus-5"
    assert delegate.MODEL_NAMES["astra"] == "gpt-6-astra"


def capacity_report(*, codex=None, claude=None, codex_score=0, claude_score=100,
                    claude_reset=None):
    codex = list(codex or [])
    claude = list(claude or [])
    return {
        "generated_at": "2026-07-22T12:00:00-04:00",
        "cache": {"checked_at": "2026-07-22T12:00:00-04:00", "hit": True,
                  "ttl_seconds": 120},
        "accounts": [*codex, *claude],
        "families": {
            "codex": {
                "score": codex_score,
                "best_resource": next(
                    (row["resource"] for row in codex if row.get("dispatchable")), None
                ),
                "earliest_reset": None,
                "dispatchable": sum(bool(row.get("dispatchable")) for row in codex),
            },
            "claude": {
                "score": claude_score,
                "best_resource": next(
                    (row["resource"] for row in claude if row.get("dispatchable")), None
                ),
                "earliest_reset": claude_reset,
                "dispatchable": sum(bool(row.get("dispatchable")) for row in claude),
            },
        },
    }


def codex_capacity_row(home="/home/codex", *, dispatchable=True, limited_until=None):
    return {
        "family": "codex", "id": Path(home).name, "email": "codex@example.com",
        "home": home, "resource": home,
        "five_hour": {"unit": "percent", "used_percent": 20 if dispatchable else 100,
                      "remaining_percent": 80 if dispatchable else 0,
                      "reset_at": limited_until},
        "weekly": {"unit": "percent", "used_percent": 30,
                   "remaining_percent": 70, "reset_at": None},
        "learned_capacity": None, "limited_until": limited_until, "confidence": "live",
        "dispatchable": dispatchable, "status": "ok",
    }


def claude_capacity_row(email, *, dispatchable=True, limited_until=None):
    return {
        "family": "claude", "id": email, "email": email, "home": None,
        "resource": email,
        "five_hour": {"unit": "tokens", "tokens": 0, "capacity": None,
                      "remaining_percent": None},
        "weekly": {"unit": "tokens", "tokens": 0, "capacity": None,
                   "remaining_percent": None},
        "learned_capacity": None, "limited_until": limited_until,
        "confidence": "estimated", "dispatchable": dispatchable,
        "status": "estimated", "enrolled": True,
    }


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    config.save(
        {
            "accounts": [
                "alpha@example.com",
                "beta@example.com",
                "charlie@example.com",
                "delta@example.com",
            ],
            "enrolled": {
                email: f"claude-quota-{email}"
                for email in (
                    "alpha@example.com",
                    "beta@example.com",
                    "charlie@example.com",
                    "delta@example.com",
                )
            },
            "codex_homes": [],
        }
    )
    state = config.state_dir()
    monkeypatch.setattr(delegate, "_active_desktop_email", lambda: None)
    lanes = [
        claude_capacity_row(email)
        for email in ("alpha@example.com", "beta@example.com", "charlie@example.com",
                      "delta@example.com")
    ]
    monkeypatch.setattr(delegate.capacity, "build", lambda: capacity_report(claude=lanes))
    return state


def test_rotation_persists_skips_cooldown_and_expiry(isolated):
    assert delegate.pick_fable_lane() == "alpha@example.com"
    assert delegate.pick_fable_lane() == "beta@example.com"
    delegate.record_cooldown("charlie@example.com", delegate._now() + timedelta(hours=1))
    assert delegate.pick_fable_lane() == "delta@example.com"
    delegate.capacity.clear_lane_cooldown("charlie@example.com")
    delegate.record_cooldown("charlie@example.com", delegate._now() - timedelta(seconds=1))
    assert delegate.pick_fable_lane() == "alpha@example.com"
    assert json.loads((isolated / "rotation.json").read_text())["last_used"] == "alpha@example.com"


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
    assert delegate.main(["--why", "Send the final email", "-o", str(isolated / "out")]) == 3
    assert len(calls) == 3
    assert len(json.loads((isolated / "cooldowns.json").read_text())) == 3
    decisions = [
        json.loads(line) for line in (isolated / "decisions.jsonl").read_text().splitlines()
    ]
    assert len(decisions) == 3
    final = decisions[-1]
    assert "fable floor stopped" in final["reason"].lower()
    assert "retry cap" in final["reason"].lower()
    assert "refusing any downgrade" in final["reason"].lower()
    assert "all claude lanes" not in final["reason"].lower()
    runtime_limited = final["capacity"]["runtime_limited_lanes"]
    assert [row["result"] for row in runtime_limited] == [4, 4, 4]
    assert {row["resource"] for row in runtime_limited} == {
        "alpha@example.com", "beta@example.com", "charlie@example.com",
    }
    err = capsys.readouterr().err.lower()
    assert "fable floor stopped" in err and "earliest reset" in err


def test_rc5_long_cooldown_and_ritual(isolated, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(5, "", "dead")]*3, calls))
    assert delegate.main(["-m", "fable", "task", "-o", str(isolated / "out")]) == 3
    err = capsys.readouterr().err
    assert "claude setup-token" in err and "claude-quota-alpha@example.com" in err
    until = next(iter(json.loads((isolated / "cooldowns.json").read_text()).values()))
    assert delegate.datetime.fromisoformat(until) > delegate._now() + timedelta(days=29)


def test_temp_paths_are_removed_when_runner_raises(isolated, tmp_path, monkeypatch):
    created = []
    real_mkstemp = delegate.tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        kwargs["dir"] = tmp_path
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(Path(path))
        return fd, path

    def fail_runner(*args, **kwargs):
        raise OSError("runner disappeared")

    monkeypatch.setattr(delegate.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(delegate.subprocess, "run", fail_runner)

    with pytest.raises(OSError, match="runner disappeared"):
        delegate.main(["Implement the endpoint"])

    delegate_created = [path for path in created if path.name.startswith("delegate-")]
    assert len(delegate_created) == 2
    assert not any(path.exists() for path in delegate_created)


def test_no_codex_capacity_overflows_automatically(isolated, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        fake_run_factory([(0, "ok", "")], calls),
    )
    out = isolated / "out"
    assert delegate.main(["For each file, check imports", "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "cross-family" in err.lower()
    assert any("subfleet-claude" in cmd[0] for cmd in calls)


def test_exhausted_codex_capacity_overflows_sweeps(isolated, monkeypatch, capsys):
    reset = (delegate._now() + timedelta(hours=1)).isoformat()
    codex_row = codex_capacity_row("/home/codex", dispatchable=False, limited_until=reset)
    lane = claude_capacity_row("alpha@example.com")
    monkeypatch.setattr(
        delegate.capacity,
        "build",
        lambda: capacity_report(codex=[codex_row], claude=[lane], claude_score=100),
    )
    seen = []

    def run(cmd, **kwargs):
        seen.append((cmd, Path(cmd[cmd.index("-p") + 1]).read_text()))
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(delegate.subprocess, "run", run)

    assert delegate.main(
        ["--why", "For each file, check imports", "-o", str(isolated / "out")]
    ) == 0

    cmd, merged_prompt = seen[0]
    assert "subfleet-claude" in cmd[0]
    assert cmd[cmd.index("-m") + 1] == delegate.MODEL_NAMES["haiku"]
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert delegate.PREAMBLE_AUDIT not in merged_prompt
    assert "cross-family overflow" in capsys.readouterr().err.lower()
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["capacity"]["scores"]["codex"]["score"] == 0
    assert decision["capacity"]["scores"]["claude"]["score"] == 100
    assert decision["family"] == "claude" and decision["cross_family"]
    assert decision["requested_family"] == "codex" and decision["routing"] is None


@pytest.mark.parametrize(
    ("prompt", "expected_sandbox", "audit_preamble"),
    [
        ("Implement the endpoint", "workspace-write", False),
        ("Audit and implement the endpoint", "read-only", True),
    ],
)
def test_legacy_review_and_build_start_on_opus(
    isolated, monkeypatch, capsys, prompt, expected_sandbox, audit_preamble
):
    """Tier-less legacy classes are standard work: Opus on a Claude lane, with
    the class's sandbox default, and no Codex involvement even when the Codex
    fleet is exhausted."""
    reset = (delegate._now() + timedelta(hours=1)).isoformat()
    codex_row = codex_capacity_row("/home/codex", dispatchable=False, limited_until=reset)
    lane = claude_capacity_row("alpha@example.com")
    monkeypatch.setattr(
        delegate.capacity,
        "build",
        lambda: capacity_report(codex=[codex_row], claude=[lane], claude_score=100),
    )
    seen = []

    def run(cmd, **kwargs):
        seen.append((cmd, Path(cmd[cmd.index("-p") + 1]).read_text()))
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(delegate.subprocess, "run", run)

    assert delegate.main(["--why", prompt, "-o", str(isolated / "out")]) == 0

    cmd, merged_prompt = seen[0]
    assert cmd[0].endswith("subfleet-claude")
    assert cmd[cmd.index("-m") + 1] == "claude-opus-5"
    assert cmd[cmd.index("-s") + 1] == expected_sandbox
    assert "-e" not in cmd
    assert (delegate.PREAMBLE_AUDIT in merged_prompt) is audit_preamble
    assert "CROSS-FAMILY" not in capsys.readouterr().err
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["model"] == decision["requested_model"] == "opus"
    assert decision["family"] == decision["requested_family"] == "claude"
    assert decision["cross_family"] is None and decision["routing"] is None


def test_all_claude_limited_fable_floor_fails_fast(isolated, monkeypatch, capsys):
    reset = (delegate._now() + timedelta(hours=2)).isoformat()
    lanes = [
        claude_capacity_row(email, dispatchable=False, limited_until=reset)
        for email in ("alpha@example.com", "beta@example.com", "charlie@example.com",
                      "delta@example.com")
    ]
    monkeypatch.setattr(
        delegate.capacity,
        "build",
        lambda: capacity_report(
            claude=lanes, claude_score=0, claude_reset=reset,
        ),
    )
    monkeypatch.setattr(
        delegate.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("floor failure dispatched a runner"),
    )

    assert delegate.main(["--why", "Send the final email", "-o", str(isolated / "out")]) == 3

    err = capsys.readouterr().err
    assert "fable floor blocked" in err.lower()
    assert "refusing to downgrade" in err.lower()
    assert reset in err
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["cmd"] == []
    assert decision["capacity"]["scores"]["claude"]["earliest_reset"] == reset


def test_preamble_defaults_off_dry_run_and_decision(isolated, monkeypatch, capsys):
    codex_row = {
        "family": "codex", "id": "codex-1", "email": "codex@example.com",
        "home": "/home/codex", "resource": "/home/codex",
        "five_hour": {"unit": "percent", "used_percent": 20,
                      "remaining_percent": 80, "reset_at": None},
        "weekly": {"unit": "percent", "used_percent": 30,
                   "remaining_percent": 70, "reset_at": None},
        "learned_capacity": None, "limited_until": None, "confidence": "live",
        "dispatchable": True, "status": "ok",
    }
    lanes = [
        claude_capacity_row(email)
        for email in ("alpha@example.com", "beta@example.com", "charlie@example.com",
                      "delta@example.com")
    ]
    monkeypatch.setattr(
        delegate.capacity,
        "build",
        lambda: capacity_report(
            codex=[codex_row], claude=lanes, codex_score=70, claude_score=100,
        ),
    )
    seen = []
    def run(cmd, **kwargs):
        if cmd[-1:] == ["--json"] or "status" in cmd:
            return CompletedProcess(cmd, 1, "", "")
        seen.append((cmd, Path(cmd[cmd.index("-p") + 1]).read_text()))
        return CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(delegate.subprocess, "run", run)
    out = isolated / "out"
    assert delegate.main(["implement x", "-o", str(out)]) == 0
    assert "Standing orders" in seen[0][1] and seen[0][0][seen[0][0].index("-s") + 1] == "workspace-write"
    assert seen[0][0][0].endswith("subfleet-claude")
    assert seen[0][0][seen[0][0].index("-m") + 1] == "claude-opus-5" and "-e" not in seen[0][0]
    seen.clear()
    assert delegate.main(["-m", "astra", "implement x", "-o", str(out)]) == 0
    assert seen[0][0][0].endswith("subfleet-codex")
    assert seen[0][0][seen[0][0].index("-m") + 1] == "gpt-6-astra"
    assert "-e" in seen[0][0] and "ultra" in seen[0][0]
    seen.clear()
    assert delegate.main(["-m", "fable", "--no-preamble", "review x", "-o", str(out)]) == 0
    assert seen[0][1] == "review x" and seen[0][0][seen[0][0].index("-s") + 1] == "read-only"
    seen.clear()
    assert delegate.main(["--dry-run", "-H", "/h", "implement x", "-o", str(out)]) == 0
    dry = capsys.readouterr().out
    assert not seen and "subfleet-codex" in dry and "gpt-6-astra" in dry and "ultra" in dry
    assert len((isolated / "decisions.jsonl").read_text().splitlines()) == 4


def test_detach_requires_output():
    with pytest.raises(SystemExit) as exc:
        delegate.main(["-d", "task"])
    assert exc.value.code == 2


def _codex_capacity(monkeypatch, *, claude=None, claude_score=100, claude_reset=None):
    monkeypatch.setattr(
        delegate.capacity,
        "build",
        lambda: capacity_report(
            codex=[codex_capacity_row("/home/codex")], claude=claude or [],
            codex_score=80, claude_score=claude_score, claude_reset=claude_reset,
        ),
    )


def _limited_claude_lanes():
    reset = (delegate._now() + timedelta(hours=2)).isoformat()
    lanes = [
        claude_capacity_row(email, dispatchable=False, limited_until=reset)
        for email in ("alpha@example.com", "beta@example.com", "charlie@example.com",
                      "delta@example.com")
    ]
    return lanes, reset


def test_astra_alias_is_gpt6_on_a_codex_lane_at_ultra_effort(isolated, monkeypatch):
    assert delegate.MODEL_FAMILY["astra"] == "codex"
    assert delegate.MODEL_NAMES["astra"] == "gpt-6-astra"
    assert "astra" in delegate.CODEX_ULTRA_EFFORT_MODELS
    _codex_capacity(monkeypatch)
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["-m", "astra", "implement x", "-o", str(isolated / "out")]) == 0
    command = calls[0]
    assert command[0].endswith("subfleet-codex")
    assert command[command.index("-H") + 1] == "/home/codex"
    assert command[command.index("-m") + 1] == "gpt-6-astra"
    assert command[command.index("-e") + 1] == "ultra"
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["overrides"]["model"] == "astra"
    assert decision["model"] == decision["requested_model"] == "astra"


def test_terra_is_not_forced_to_ultra_effort(isolated, monkeypatch):
    _codex_capacity(monkeypatch)
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["-m", "terra", "implement x", "-o", str(isolated / "out")]) == 0
    assert calls[0][calls[0].index("-m") + 1] == "gpt-5.6-terra"
    assert "-e" not in calls[0]


def test_retired_sol_alias_dispatches_astra_and_says_so(isolated, monkeypatch, capsys):
    assert delegate.RETIRED_MODEL_ALIASES == {"sol": "astra"}
    _codex_capacity(monkeypatch)
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
    assert decision["model"] == decision["requested_model"] == "astra"  # what ran


def test_no_automatic_route_selects_sol():
    for task_class in ("fable", "review", "sweep", "build"):
        assert delegate.choose_model(task_class) != "sol"
    assert "sol" not in delegate.CLAUDE_OVERFLOW_MODEL.values()
    assert delegate.STANDARD_UPWARD_MODEL != "sol"
    assert delegate.CODEX_HOME_DEFAULT_MODEL != "sol"


def test_codex_home_pin_without_model_implies_astra(isolated, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["-H", "/pinned", "implement x", "-o", str(isolated / "out")]) == 0
    command = calls[0]
    assert command[0].endswith("subfleet-codex")
    assert command[command.index("-H") + 1] == "/pinned"
    assert command[command.index("-m") + 1] == "gpt-6-astra"
    assert command[command.index("-e") + 1] == "ultra"
    assert "CROSS-FAMILY" not in capsys.readouterr().err
    decision = json.loads((isolated / "decisions.jsonl").read_text())
    assert decision["overrides"] == {"home": "/pinned"}
    assert decision["model"] == decision["requested_model"] == "astra"


def test_lane_pins_reject_the_other_family_with_all_aliases_named(isolated, capsys):
    with pytest.raises(SystemExit) as exc:
        delegate.main(["-H", "/pinned", "-m", "opus", "task", "-o", str(isolated / "out")])
    assert exc.value.code == 2
    assert "sol, terra, astra" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        delegate.main(["-a", "alpha@example.com", "-m", "astra", "task", "-o", str(isolated / "out")])
    assert exc.value.code == 2
    assert "-a is only valid with a Claude model" in capsys.readouterr().err


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
    assert delegate.main(
        ["-H", str(lane), "-m", "astra", "implement x", "-o", str(isolated / "out")]
    ) == 0
    assert calls[0][calls[0].index("-H") + 1] == str(lane)


def test_legacy_build_moves_upward_to_astra_only_when_no_claude_lane_is_dispatchable(
        isolated, monkeypatch, capsys):
    lanes, reset = _limited_claude_lanes()
    _codex_capacity(monkeypatch, claude=lanes, claude_score=0, claude_reset=reset)
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "", "")], calls))
    assert delegate.main(["--why", "implement x", "-o", str(isolated / "out")]) == 0
    err = capsys.readouterr().err
    assert "CAPABILITY FALLBACK" in err and "build/standard upward to astra" in err
    assert "CROSS-FAMILY" not in err
    command = calls[0]
    assert command[0].endswith("subfleet-codex")
    assert command[command.index("-H") + 1] == "/home/codex"
    assert command[command.index("-m") + 1] == "gpt-6-astra"
    assert command[command.index("-e") + 1] == "ultra"
    assert command[command.index("-s") + 1] == "workspace-write"
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["requested_model"] == "opus" and decision["model"] == "astra"
    assert decision["requested_family"] == "claude" and decision["family"] == "codex"
    assert "upward to astra" in decision["routing"]


def test_legacy_review_moves_upward_read_only_with_audit_frame(isolated, monkeypatch, capsys):
    lanes, reset = _limited_claude_lanes()
    _codex_capacity(monkeypatch, claude=lanes, claude_score=0, claude_reset=reset)
    seen = []

    def run(cmd, **kwargs):
        seen.append((cmd, Path(cmd[cmd.index("-p") + 1]).read_text()))
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(delegate.subprocess, "run", run)
    assert delegate.main(["Review this patch", "-o", str(isolated / "out")]) == 0
    assert "review/standard upward to astra" in capsys.readouterr().err
    cmd, merged_prompt = seen[0]
    assert cmd[0].endswith("subfleet-codex")
    assert cmd[cmd.index("-m") + 1] == "gpt-6-astra"
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert delegate.PREAMBLE_AUDIT in merged_prompt


def test_legacy_build_moves_upward_after_runtime_claude_limits(isolated, monkeypatch, capsys):
    """Capacity said Claude was fine, but every lane answered rc 4 at dispatch
    time: after the retry cap the work moves upward to Astra instead of failing."""
    lanes = [
        claude_capacity_row(email)
        for email in ("alpha@example.com", "beta@example.com", "charlie@example.com",
                      "delta@example.com")
    ]
    _codex_capacity(monkeypatch, claude=lanes)
    calls = []
    monkeypatch.setattr(
        delegate.subprocess, "run",
        fake_run_factory([(4, "", "limit resets 3:15pm")] * 3 + [(0, "", "")], calls),
    )
    assert delegate.main(["--why", "implement x", "-o", str(isolated / "out")]) == 0
    assert len(calls) == 4
    assert all(cmd[0].endswith("subfleet-claude") for cmd in calls[:3])
    assert calls[3][0].endswith("subfleet-codex")
    assert calls[3][calls[3].index("-m") + 1] == "gpt-6-astra"
    err = capsys.readouterr().err
    assert "CAPABILITY FALLBACK" in err and "retry cap reached" in err
    decisions = [
        json.loads(line) for line in (isolated / "decisions.jsonl").read_text().splitlines()
    ]
    assert [row["result"] for row in decisions] == [4, 4, 4, 0]
    assert [row["model"] for row in decisions] == ["opus", "opus", "opus", "astra"]
    assert decisions[-1]["requested_model"] == "opus"
    assert [row["result"] for row in decisions[-1]["capacity"]["runtime_limited_lanes"]] == [4, 4, 4]
    assert len(json.loads((isolated / "cooldowns.json").read_text())) == 3


def test_legacy_build_fails_in_place_when_no_codex_lane_can_take_it(isolated, monkeypatch, capsys):
    lanes, reset = _limited_claude_lanes()
    monkeypatch.setattr(
        delegate.capacity, "build",
        lambda: capacity_report(claude=lanes, claude_score=0, claude_reset=reset),
    )
    monkeypatch.setattr(
        delegate.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("no lane was dispatchable"),
    )
    assert delegate.main(["implement x", "-o", str(isolated / "out")]) == 3
    err = capsys.readouterr().err
    assert "CAPABILITY FALLBACK" not in err and "CROSS-FAMILY" not in err
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["model"] == "opus" and decision["cmd"] == [] and decision["result"] == 3


def test_fable_floor_never_moves_upward_to_astra(isolated, monkeypatch, capsys):
    lanes, reset = _limited_claude_lanes()
    _codex_capacity(monkeypatch, claude=lanes, claude_score=0, claude_reset=reset)
    monkeypatch.setattr(
        delegate.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("floor work was dispatched"),
    )
    assert delegate.main(["Send the final email", "-o", str(isolated / "out")]) == 3
    err = capsys.readouterr().err
    assert "fable floor blocked" in err.lower() and "CAPABILITY FALLBACK" not in err


def test_pinned_claude_lane_never_moves_upward(isolated, monkeypatch, capsys):
    _codex_capacity(monkeypatch, claude=[claude_capacity_row("alpha@example.com")])
    calls = []
    monkeypatch.setattr(
        delegate.subprocess, "run", fake_run_factory([(4, "", "limit resets 3:15pm")], calls)
    )
    assert delegate.main(
        ["-a", "alpha@example.com", "implement x", "-o", str(isolated / "out")]
    ) == 3
    assert len(calls) == 1 and calls[0][0].endswith("subfleet-claude")
    assert "CAPABILITY FALLBACK" not in capsys.readouterr().err

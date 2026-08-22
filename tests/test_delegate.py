import json
from datetime import timedelta
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from carpool import config, delegate


@pytest.mark.parametrize(("prompt", "kind", "model"), [
    ("Answer as Max", "fable", "fable"),
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
    ("Review this patch", "review", "sol"),
    ("Assess this patch", "review", "sol"),
    ("Critique this patch", "review", "sol"),
    ("Audit this patch", "review", "sol"),
    ("Evaluate this patch", "review", "sol"),
    ("Referee this dispute", "review", "sol"),
    ("Final review before we implement the fix", "fable", "fable"),
    ("As Max, for each file check and implement it", "fable", "fable"),
    ("Audit and implement the fix", "review", "sol"),
    ("For each file, check imports", "sweep", "terra"),
    ("Extract ids across all rows", "sweep", "terra"),
    ("Count a batch of records", "sweep", "terra"),
    ("For each feature implement it", "build", "sol"),
    ("Implement the voicemail redesign launcher", "build", "sol"),
    ("Implement the endpoint", "build", "sol"),
    ("Fix and test the bug", "build", "sol"),
    ("Refactor the parser", "build", "sol"),
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
    assert "refusing any sol downgrade" in final["reason"].lower()
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
    assert delegate.main(["implement x", "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "cross-family" in err.lower()
    assert any("carpool-claude" in cmd[0] for cmd in calls)


@pytest.mark.parametrize(
    ("prompt", "expected_sandbox", "audit_preamble"),
    [
        ("Implement the endpoint", "workspace-write", False),
        ("Audit and implement the endpoint", "read-only", True),
    ],
)
def test_exhausted_codex_capacity_overflows_elastic_classes(
    isolated, monkeypatch, capsys, prompt, expected_sandbox, audit_preamble
):
    reset = (delegate._now() + timedelta(hours=1)).isoformat()
    codex_row = {
        "family": "codex", "id": "codex-1", "email": "codex@example.com",
        "home": "/home/codex", "resource": "/home/codex",
        "five_hour": {"unit": "percent", "used_percent": 100,
                      "remaining_percent": 0, "reset_at": reset},
        "weekly": {"unit": "percent", "used_percent": 50,
                   "remaining_percent": 50, "reset_at": None},
        "learned_capacity": None, "limited_until": reset, "confidence": "live",
        "dispatchable": False, "status": "ok",
    }
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
    assert "carpool-claude" in cmd[0]
    assert cmd[cmd.index("-m") + 1] == delegate.MODEL_NAMES["fable"]
    assert cmd[cmd.index("-s") + 1] == expected_sandbox
    assert (delegate.PREAMBLE_AUDIT in merged_prompt) is audit_preamble
    assert "cross-family overflow" in capsys.readouterr().err.lower()
    decision = json.loads((isolated / "decisions.jsonl").read_text().splitlines()[-1])
    assert decision["capacity"]["scores"]["codex"]["score"] == 0
    assert decision["capacity"]["scores"]["claude"]["score"] == 100
    assert decision["family"] == "claude" and decision["cross_family"]


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
        if "codex-pick" in cmd[0]:
            return CompletedProcess(cmd, 0, "/home/codex\n", "")
        seen.append((cmd, Path(cmd[cmd.index("-p") + 1]).read_text()))
        return CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(delegate.subprocess, "run", run)
    out = isolated / "out"
    assert delegate.main(["implement x", "-o", str(out)]) == 0
    assert "Standing orders" in seen[0][1] and seen[0][0][seen[0][0].index("-s") + 1] == "workspace-write"
    assert "-e" in seen[0][0] and "ultra" in seen[0][0]
    seen.clear()
    assert delegate.main(["-m", "fable", "--no-preamble", "review x", "-o", str(out)]) == 0
    assert seen[0][1] == "review x" and seen[0][0][seen[0][0].index("-s") + 1] == "read-only"
    seen.clear()
    assert delegate.main(["--dry-run", "-H", "/h", "implement x", "-o", str(out)]) == 0
    assert not seen and "carpool-codex" in capsys.readouterr().out
    assert len((isolated / "decisions.jsonl").read_text().splitlines()) == 3


def test_detach_requires_output():
    with pytest.raises(SystemExit) as exc:
        delegate.main(["-d", "task"])
    assert exc.value.code == 2

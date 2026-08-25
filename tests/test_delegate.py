import json
import time
from datetime import timedelta
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from subfleet import config, delegate

SUBFLEET_BIN = Path(__file__).resolve().parents[1] / "bin" / "subfleet"


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
    ("Review this patch", "review", "sol"),
    ("Assess this patch", "review", "sol"),
    ("Critique this patch", "review", "sol"),
    ("Audit this patch", "review", "sol"),
    ("Evaluate this patch", "review", "sol"),
    ("Referee this dispute", "review", "sol"),
    ("Final review before we implement the fix", "fable", "fable"),
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
    assert any("subfleet-claude" in cmd[0] for cmd in calls)


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
    assert "subfleet-claude" in cmd[0]
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
    assert not seen and "subfleet-codex" in capsys.readouterr().out
    assert len((isolated / "decisions.jsonl").read_text().splitlines()) == 3


def test_detach_without_output_hosts_it_in_the_ledger(isolated, tmp_path, monkeypatch, capsys):
    """`-d` needs no -o: the run directory hosts out.md and the err/lane logs."""
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
    monkeypatch.setenv("SUBFLEET_TEST_BIN", str(SUBFLEET_BIN))

    assert delegate.main(["-d", "-H", "/home/codex", "-C", str(tmp_path), "-n", "hosted", "task"]) == 0
    banner = capsys.readouterr().out
    assert "subfleet run: dispatched run=" in banner
    run_id = banner.split("run=", 1)[1].split()[0]
    assert run_id.endswith("-hosted")
    run_dir = ledger_state / "runs" / run_id
    assert f"out: {run_dir / 'out.md'}" in banner
    assert f"log: {run_dir / 'out.lane.log'}" in banner

    deadline = time.monotonic() + 20  # generous: suites run under heavy load
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
    monkeypatch.setenv("SUBFLEET_TEST_BIN", str(SUBFLEET_BIN))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "11111111-2222-4333-8444-555555555555")

    started = time.monotonic()
    assert delegate.main(["-H", "/home/codex", "-C", str(tmp_path), "-o", str(tmp_path / "a.md"), "task"]) == 0
    assert time.monotonic() - started < 1
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

    meta = json.loads(next((tmp_path / "ledger-state" / "runs").glob("*/meta.json")).read_text())
    assert meta["caller"]["session_id"] == "11111111-2222-4333-8444-555555555555"


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


def test_codex_detach_returns_before_runner_finishes(isolated, tmp_path, monkeypatch, capsys):
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
printf 'fake runner finished\\n'
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
    assert "model=sol" in first and "lane=/home/codex" in first and "(detached — -d)" in first
    pid = int(first.split("pid=", 1)[1].split()[0])
    assert pid > 0
    assert f"  out: {output}" in message
    assert f"  log: {lane_log}" in message
    assert "subfleet wait " in message

    deadline = time.monotonic() + 20  # generous: suites run under heavy load
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


@pytest.mark.parametrize("detached", [False, True])
def test_real_codex_runner_ledgers_delegate_decision_and_detached_log(
    isolated, tmp_path, monkeypatch, detached
):
    import os

    fake_bin = tmp_path / "delegate-bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        """#!/bin/bash
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then out=$2; shift 2; else shift; fi
done
printf 'delegated result\\n' > "$out"
"""
    )
    fake_codex.chmod(0o755)
    runner = Path(__file__).resolve().parents[1] / "bin" / "subfleet-codex"
    ledger_state = tmp_path / "ledger-state"
    workdir = tmp_path / "delegated-work"
    workdir.mkdir()
    home = tmp_path / "codex-lane"
    home.mkdir()
    output = tmp_path / "delegated-answer.md"
    codex_row = {
        "family": "codex", "id": "codex-1", "email": "codex@example.com",
        "home": str(home), "resource": str(home),
        "five_hour": {"unit": "percent", "used_percent": 10,
                      "remaining_percent": 90, "reset_at": None},
        "weekly": {"unit": "percent", "used_percent": 10,
                   "remaining_percent": 90, "reset_at": None},
        "learned_capacity": None, "limited_until": None, "confidence": "live",
        "dispatchable": True, "status": "ok",
    }
    monkeypatch.setattr(
        delegate.capacity, "build",
        lambda: capacity_report(codex=[codex_row], codex_score=90, claude_score=100),
    )
    monkeypatch.setenv("DELEGATE_CODEX_RUN", str(runner))
    monkeypatch.setenv("SUBFLEET_CODEX_GUARD", "off")
    monkeypatch.setenv("SUBFLEET_CODEX_BIN", str(fake_codex))
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(ledger_state))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    argv = [
        "-m", "sol", "-C", str(workdir), "-o", str(output),
        "implement delegated ledger",
    ]
    if detached:
        argv.insert(0, "-d")
    assert delegate.main(argv) == 0

    deadline = time.monotonic() + 10
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
    assert meta["routing_decision"]["cmd"][0] == str(runner)
    saved_prompt = (run_dir / "prompt.md").read_text()
    assert delegate.PREAMBLE_WRITE in saved_prompt
    assert saved_prompt.endswith("implement delegated ledger")
    assert (run_dir / "out.md").read_text() == "delegated result\n"
    if detached:
        assert "subfleet codex: OK" in (run_dir / "lane.log").read_text()

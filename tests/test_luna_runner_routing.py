"""Synthetic provider -> actual shell runner -> delegate routing contracts.

No account files, provider calls, reset credits, or live capacity probes.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import delegate, handoff, paths, run_ledger
from test_delegate import capacity_row, capacity_snapshot, isolated  # noqa: F401
from test_handoff import SESSION, _entry, _transcript
from test_reserve import _install_readings
from test_run_ledger_codex_runner import SCRIPT, _worktree_subfleet


@pytest.fixture
def harness(tmp_path, monkeypatch, isolated):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_file = tmp_path / "provider-calls.jsonl"
    provider = fake_bin / "codex"
    provider.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "home = os.environ['CODEX_HOME']\n"
        "with open(os.environ['TEST_PROVIDER_CALLS'], 'a') as f:\n"
        "    f.write(json.dumps({'home': home, 'argv': sys.argv[1:]}) + '\\n')\n"
        "rc = int(json.loads(os.environ['TEST_HOME_RESULTS']).get(home, 0))\n"
        "if rc:\n"
        "    print(os.environ.get('TEST_FAILURE_TEXT', \"You've hit your usage limit\"), file=sys.stderr)\n"
        "    sys.exit(rc)\n"
        "Path(sys.argv[sys.argv.index('-o') + 1]).write_text('synthetic success\\n')\n"
    )
    provider.chmod(0o755)
    picker = fake_bin / "pick"
    picker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "p = Path(os.environ['DELEGATE_STATE_DIR']) / 'cooldowns.json'\n"
        "cooled = json.loads(p.read_text()) if p.exists() else {}\n"
        "for home in json.loads(os.environ['TEST_HOMES']):\n"
        "    if home not in cooled:\n"
        "        print(home)\n"
        "        sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    picker.chmod(0o755)
    cli = _worktree_subfleet(fake_bin / "test-subfleet")
    homes = [tmp_path / f"codex-{i}" for i in range(4)]
    for home in homes:
        home.mkdir()
        (home / "config.toml").write_text('model_reasoning_effort = "ultra"\n')
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SUBFLEET_CODEX_GUARD", "off")
    monkeypatch.setenv("SUBFLEET_RUN_SUBFLEET", str(cli))
    monkeypatch.setenv("SUBFLEET_CODEX_PICK", str(picker))
    monkeypatch.setenv("SUBFLEET_CODEX_HOMES", "")
    monkeypatch.setenv("TEST_PROVIDER_CALLS", str(calls_file))
    monkeypatch.setenv("TEST_HOMES", json.dumps([str(h) for h in homes]))
    monkeypatch.setenv("TEST_HOME_RESULTS", "{}")
    rows = [capacity_row("codex", str(home)) for home in homes]
    snapshot = capacity_snapshot(codex_rows=rows, claude_rows=[capacity_row("claude", "lane@x")])
    refreshes = []
    def capacity_report():
        refreshes.append(True)
        return snapshot  # deliberately stale dispatchable flags after cooldown
    monkeypatch.setattr(delegate, "_capacity_report", capacity_report)
    def provider_calls():
        return [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
    return SimpleNamespace(
        homes=homes, rows=rows, snapshot=snapshot, refreshes=refreshes,
        workdir=workdir, state=isolated, provider_calls=provider_calls,
        picker=picker, cli=cli, tmp=tmp_path,
    )


def dispatch(harness, *options):
    return delegate.main([
        *options, "--attach", "--no-preamble", "synthetic task", "-C", str(harness.workdir),
        "-o", str(harness.tmp / "out.md"),
    ])


def decisions(harness):
    return [json.loads(line) for line in (harness.state / "decisions.jsonl").read_text().splitlines()]


def test_retry_budget_does_not_exhaust_four_home_pool(harness, monkeypatch):
    monkeypatch.setenv("TEST_HOME_RESULTS", json.dumps({str(h): 9 for h in harness.homes[:3]}))
    assert dispatch(harness, "--task", "lookup", "--tier", "trivial") == 8
    assert [c["home"] for c in harness.provider_calls()] == [str(h) for h in harness.homes[:3]]
    record = decisions(harness)[0]
    assert record["routing_history"] == []
    assert record["routing_capacity_states"]["luna"] is True
    assert record["routing_capacity_states"]["astra"] is True
    assert len(harness.refreshes) == 2
    assert dispatch(harness, "--task", "lookup", "--tier", "trivial") == 0
    assert [c["home"] for c in harness.provider_calls()] == [str(h) for h in harness.homes]
    assert [r["result"] for r in decisions(harness)] == [8, 0]
    assert all(r["model"] == "luna" for r in decisions(harness))


@pytest.mark.parametrize("home_count, quota_rc", [(2, 4), (3, 8)])
def test_runtime_quota_then_reserved_opus_runs_fable(harness, monkeypatch, home_count, quota_rc):
    harness.snapshot["accounts"] = harness.rows[:home_count] + [capacity_row("claude", "lane@x")]
    monkeypatch.setenv("TEST_HOMES", json.dumps([str(h) for h in harness.homes[:home_count]]))
    monkeypatch.setenv("TEST_HOME_RESULTS", json.dumps({str(h): 9 for h in harness.homes[:home_count]}))
    paths.reserve_policy_path().write_text('{"enabled": true}')
    _install_readings(monkeypatch, {"lane@x": ("reserved", "ok", -0.5)})
    actual_run = subprocess.run
    launched = []
    def run(cmd, **kwargs):
        if "-m" in cmd:
            launched.append(cmd[cmd.index("-m") + 1])
            if launched[-1] == delegate.MODEL_NAMES["fable"]:
                return subprocess.CompletedProcess(cmd, 0, "success", "")
        return actual_run(cmd, **kwargs)
    monkeypatch.setattr(delegate.subprocess, "run", run)
    assert dispatch(harness, "--task", "lookup", "--tier", "easy") == 0
    assert launched == ["gpt-5.6-luna", "claude-fable-5-1"]
    assert len(harness.provider_calls()) == home_count
    records = decisions(harness)
    assert [r["result"] for r in records] == [quota_rc, 0]
    assert [(r["from"], r["to"]) for r in records[-1]["routing_history"]] == [
        ("luna", "opus"), ("opus", "fable")
    ]
    assert records[-1]["routing_capacity_states"]["astra"] is False
    assert records[-1]["reserve"]["action"] == "upgraded to fable"


@pytest.mark.parametrize("home_count, quota_rc", [(1, 4), (3, 8)])
def test_unknown_refresh_is_not_evidence_of_fleet_exhaustion(harness, monkeypatch, home_count, quota_rc):
    monkeypatch.setenv("TEST_HOMES", json.dumps([str(h) for h in harness.homes[:home_count]]))
    monkeypatch.setenv("TEST_HOME_RESULTS", json.dumps({str(h): 9 for h in harness.homes[:home_count]}))
    unknown = capacity_snapshot(codex_rows=[capacity_row(
        "codex", "/synthetic/unknown", dispatchable=False, status="network-error"
    )])
    reports = iter([harness.snapshot, unknown])
    monkeypatch.setattr(delegate, "_capacity_report", lambda: next(reports))
    assert dispatch(harness, "--task", "lookup", "--tier", "trivial") == quota_rc
    assert decisions(harness)[0]["routing_history"] == []
    assert decisions(harness)[0]["routing_capacity_states"]["luna"] is None
    assert decisions(harness)[0]["routing_capacity_states"]["astra"] is None


def test_dispatchable_home_wins_over_stale_limited_summary(harness):
    harness.snapshot["families"]["codex"]["all_limited"] = True
    assert delegate._model_capacity_state(harness.snapshot, "luna") is True
    assert delegate._model_capacity_state(harness.snapshot, "astra") is True


@pytest.mark.parametrize("provider_rc", [4, 8, 9])
def test_ordinary_provider_errors_never_promote(harness, monkeypatch, provider_rc):
    monkeypatch.setenv("TEST_HOME_RESULTS", json.dumps({str(harness.homes[0]): provider_rc}))
    monkeypatch.setenv("TEST_FAILURE_TEXT", "ordinary parse failure")
    assert dispatch(harness, "--task", "lookup", "--tier", "easy") == (1 if provider_rc in (4, 8) else 9)
    assert len(harness.provider_calls()) == 1
    assert decisions(harness)[0]["routing_history"] == []
    assert not (harness.state / "cooldowns.json").exists()


@pytest.mark.parametrize("options, effort, model", [
    (["--task", "lookup", "--tier", "trivial"], "low", "gpt-5.6-luna"),
    (["--task", "lookup", "--tier", "easy"], "medium", "gpt-5.6-luna"),
    (["-m", "luna"], "medium", "gpt-5.6-luna"),
    (["-m", "luna", "-H", "{home}"], "medium", "gpt-5.6-luna"),
    (["-H", "{home}"], "ultra", "gpt-6-astra"),
    (["-H", "{home}", "--task", "lookup", "--tier", "trivial"], "low", "gpt-5.6-luna"),
])
def test_effort_overrides_ultra_home_config(harness, options, effort, model):
    options = [str(harness.homes[0]) if arg == "{home}" else arg for arg in options]
    assert dispatch(harness, *options) == 0
    argv = harness.provider_calls()[0]["argv"]
    assert argv[argv.index("-m") + 1] == model
    assert f'model_reasoning_effort="{effort}"' in argv
    assert sum(arg.startswith("model_reasoning_effort=") for arg in argv) == 1


@pytest.mark.parametrize("home_pin", [False, True])
def test_luna_pin_does_not_promote_on_quota(harness, monkeypatch, home_pin):
    monkeypatch.setenv("TEST_HOME_RESULTS", json.dumps({str(h): 9 for h in harness.homes}))
    options = ["-m", "luna"] + (["-H", str(harness.homes[0])] if home_pin else [])
    assert dispatch(harness, *options) == (4 if home_pin else 8)
    assert len(harness.provider_calls()) == (1 if home_pin else 3)
    assert decisions(harness)[0]["routing_history"] == []


def test_luna_pin_with_no_capacity_does_not_launch(harness):
    for row in harness.rows:
        row.update(dispatchable=False, status="limited")
    assert dispatch(harness, "-m", "luna") == 3
    assert harness.provider_calls() == []


@pytest.mark.parametrize("fault", ["picker", "empty-picker", "same-home", "cooldown"])
def test_routing_infrastructure_failure_is_not_quota(harness, monkeypatch, fault):
    monkeypatch.setenv("TEST_HOME_RESULTS", json.dumps({str(harness.homes[0]): 9}))
    if fault == "picker":
        harness.picker.write_text("#!/bin/bash\nexit 2\n")
    elif fault == "empty-picker":
        harness.picker.write_text("#!/bin/bash\nexit 0\n")
    elif fault == "same-home":
        harness.picker.write_text(f"#!/bin/bash\nprintf '%s\\n' {shlex.quote(str(harness.homes[0]))}\n")
    else:
        # Keep ledger operations real; fail only the cooldown write.
        original = harness.cli.read_text()
        harness.cli.write_text(original.replace(
            "#!/bin/bash\n", '#!/bin/bash\n[ "$1" != "_record-codex-cooldown" ] || exit 2\n', 1
        ))
    assert dispatch(harness, "--task", "lookup", "--tier", "easy") == 1
    assert len(harness.provider_calls()) == 1
    assert decisions(harness)[0]["routing_history"] == []


def test_rotated_home_guard_failure_is_not_quota(harness, monkeypatch):
    monkeypatch.setenv("TEST_HOME_RESULTS", json.dumps({str(harness.homes[0]): 9}))
    runners = harness.tmp / "runners"
    runners.mkdir()
    runner = runners / "subfleet-codex"
    runner.write_bytes(SCRIPT.read_bytes())
    runner.chmod(0o755)
    guard = runners / "subfleet-guard"
    guard.write_text(
        '#!/bin/bash\n[ "$1" != override ] || { echo hooks=[]; exit 0; }\n'
        f'[ "$3" != {shlex.quote(str(harness.homes[1]))} ]\n'
    )
    guard.chmod(0o755)
    monkeypatch.setenv("DELEGATE_CODEX_RUN", str(runner))
    monkeypatch.setenv("SUBFLEET_CODEX_GUARD", "on")
    assert dispatch(harness, "--task", "lookup", "--tier", "easy") == 2
    assert len(harness.provider_calls()) == 1
    assert decisions(harness)[0]["routing_history"] == []


def test_explicit_sweep_runtime_quota_keeps_terra(harness, monkeypatch):
    harness.snapshot["accounts"] = harness.rows[:1] + [capacity_row("claude", "lane@x")]
    monkeypatch.setenv("TEST_HOMES", json.dumps([str(harness.homes[0])]))
    monkeypatch.setenv("TEST_HOME_RESULTS", json.dumps({str(harness.homes[0]): 9}))
    assert dispatch(harness, "--task", "sweep", "--tier", "easy") == 4
    assert len(harness.provider_calls()) == 1
    assert decisions(harness)[0]["model"] == "terra"
    assert decisions(harness)[0]["routing_history"] == []


@pytest.mark.parametrize("wait_inline", [False, True])
def test_detached_quota_keeps_model_and_prompt_ownership(harness, monkeypatch, wait_inline):
    launched = []
    actual_popen = subprocess.Popen
    def popen(cmd, **kwargs):
        if "SUBFLEET_RUN_OWNED_PROMPT" not in kwargs.get("env", {}):
            return actual_popen(cmd, **kwargs)
        launched.append(cmd)
        # Simulate the child completing and deleting the transferred prompt.
        Path(kwargs["env"]["SUBFLEET_RUN_OWNED_PROMPT"]).unlink()
        return SimpleNamespace(pid=99999999)
    monkeypatch.setattr(delegate.subprocess, "Popen", popen)
    monkeypatch.setattr(run_ledger, "wait_for_runs", lambda ids: {ids[0]: {"rc": 4}})
    options = ["--wait"] if wait_inline else []
    assert delegate.main([
        "-d", *options, "--task", "lookup", "--tier", "trivial", "task",
        "-C", str(harness.workdir), "-o", str(harness.tmp / "detached.md"),
    ]) == (4 if wait_inline else 0)
    assert len(launched) == 1
    assert launched[0][launched[0].index("-m") + 1] == "gpt-5.6-luna"
    assert decisions(harness)[0]["routing_history"] == []


def test_luna_handoff_reaches_delegate_with_explicit_medium_effort(harness, monkeypatch):
    _transcript(Path(os.environ["SUBFLEET_CLAUDE_DIR"]), SESSION, [
        _entry("user", "Continue the synthetic task.", row_uuid="u1", cwd=str(harness.workdir))
    ])
    launched = []
    actual_popen = subprocess.Popen
    def popen(cmd, **kwargs):
        if "SUBFLEET_RUN_OWNED_PROMPT" not in kwargs.get("env", {}):
            return actual_popen(cmd, **kwargs)
        launched.append(cmd)
        Path(kwargs["env"]["SUBFLEET_RUN_OWNED_PROMPT"]).unlink()
        return SimpleNamespace(pid=99999999)
    monkeypatch.setattr(delegate.subprocess, "Popen", popen)
    assert handoff.run(SESSION, False, "luna", None, delegate_main=delegate.main) == 0
    assert len(launched) == 1
    assert launched[0][launched[0].index("-m") + 1] == "gpt-5.6-luna"
    assert launched[0][launched[0].index("-e") + 1] == "medium"

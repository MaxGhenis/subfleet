"""Fable reserve: verdict math, readings plumbing, and the dispatch guard.

The incident that motivated the rule (2026-09-06, farness/policybench at 94%
shared / 49% Fable; 2026-09-08 axiom.org 100/27) is the first fixture.
"""

import hashlib
import json
import time
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from subfleet import delegate, paths, reserve
from test_delegate import capacity_row, capacity_snapshot, fake_run_factory


# --- verdict ------------------------------------------------------------------

@pytest.mark.parametrize(("shared", "fable", "state"), [
    (94.0, 49.0, "reserved"),   # 2026-09-06 incident account
    (100.0, 27.0, "reserved"),  # 2026-09-08 axiom.org
    (0.0, 0.0, "reserved"),     # untouched account: Fable goes first
    (10.0, 40.0, "reserved"),
    (70.0, 90.0, "slack"),      # 0.30 - 2*0.10 = 0.10 >= 0.05
    (79.0, 90.0, "slack"),      # 0.21 - 0.20 = 0.01 < 0.05 -> below; see next
])
def test_verdict_states(shared, fable, state):
    pol = dict(reserve.DEFAULT_POLICY)
    got = reserve.verdict(shared, fable, pol=pol)
    if (shared, fable) == (79.0, 90.0):
        assert got["state"] == "reserved" and got["slack"] == pytest.approx(0.01)
    else:
        assert got["state"] == state


def test_verdict_slack_formula_matches_v2():
    got = reserve.verdict(94.0, 49.0, pol=dict(reserve.DEFAULT_POLICY))
    assert got["slack"] == pytest.approx((1 - 0.94) - 2.0 * (1 - 0.49), abs=1e-4)


def test_unmeasured_and_no_fable_states():
    pol = dict(reserve.DEFAULT_POLICY)
    assert reserve.verdict(None, None, pol=pol)["state"] == "unmeasured"
    assert reserve.verdict(45.0, None, fable_present=True, pol=pol)["state"] == "unmeasured"
    # A Team seat reports no Fable window: nothing to protect.
    assert reserve.verdict(45.0, None, fable_present=False, pol=pol)["state"] == "no-fable"


def test_policy_file_overrides_and_ignores_junk(tmp_path):
    path = tmp_path / "reserve-policy.json"
    path.write_text(json.dumps({"cap_ratio": 1.5, "enabled": False, "bogus": 1, "min_slack": "x"}))
    pol = reserve.policy(path)
    assert pol["cap_ratio"] == 1.5 and pol["enabled"] is False
    assert pol["min_slack"] == reserve.DEFAULT_POLICY["min_slack"]
    assert "bogus" not in pol
    assert reserve.policy(tmp_path / "missing.json")["enabled"] is True


# --- parse --------------------------------------------------------------------

def _probe(shared=45.0, fable=76.0, *, with_fable=True):
    limits = [
        {"kind": "session", "percent": 100, "scope_model": None},
        {"kind": "weekly_all", "percent": shared, "scope_model": None,
         "resets_at": "2026-09-15T05:00:00+00:00"},
    ]
    if with_fable:
        limits.append({"kind": "weekly_scoped", "percent": fable, "scope_model": "Fable"})
    return {"status": "ok", "limits": limits,
            "seven_day": {"used_percent": shared, "reset_at": "2026-09-15T05:00:00+00:00"}}


def test_parse_reading_reads_both_windows():
    got = reserve.parse_reading(_probe(39.0, 76.0))
    assert (got["shared"], got["fable"], got["fable_present"]) == (39.0, 76.0, True)
    assert got["resets_at"].startswith("2026-09-15")


def test_parse_reading_without_fable_window_flags_absence():
    got = reserve.parse_reading(_probe(45.0, with_fable=False))
    assert got["shared"] == 45.0 and got["fable"] is None and got["fable_present"] is False


# --- login token ----------------------------------------------------------------

def test_keychain_service_hashes_exact_path():
    home = Path("/Users/max/.subfleet/logins/a@x")
    digest = hashlib.sha256(str(home).encode()).hexdigest()[:8]
    assert reserve.keychain_service(home) == f"Claude Code-credentials-{digest}"
    assert reserve.keychain_service(str(home) + "/") != reserve.keychain_service(home)


def test_login_token_states(tmp_path, monkeypatch):
    logins = tmp_path / "logins"
    monkeypatch.setenv("SUBFLEET_CLAUDE_LOGINS", str(logins))
    assert reserve.login_token("a@x") == ("no-login", None)
    (logins / "a@x").mkdir(parents=True)
    now_ms = 1_000_000

    def runner_factory(payload, rc=0):
        def runner(cmd, **kwargs):
            assert cmd[:3] == ["security", "find-generic-password", "-s"]
            assert cmd[3] == reserve.keychain_service(logins / "a@x")
            return CompletedProcess(cmd, rc, payload, "")
        return runner

    assert reserve.login_token("a@x", runner=runner_factory("", rc=44)) == ("no-credential", None)
    live = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-LIVE", "expiresAt": now_ms + 1}})
    assert reserve.login_token("a@x", runner=runner_factory(live), now_ms=now_ms) == ("ok", "sk-ant-oat01-LIVE")
    stale = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-OLD", "expiresAt": now_ms - 1}})
    assert reserve.login_token("a@x", runner=runner_factory(stale), now_ms=now_ms) == ("expired-token", None)
    assert reserve.login_token("a@x", runner=runner_factory("{not json"))[0] == "unparseable"


# --- readings -------------------------------------------------------------------

@pytest.fixture
def logins(tmp_path, monkeypatch):
    root = tmp_path / "logins"
    monkeypatch.setenv("SUBFLEET_CLAUDE_LOGINS", str(root))
    for email in ("a@x", "b@x", "c@x"):
        (root / email).mkdir(parents=True)
    return root


def _token_runner(tokens: dict[str, str], now_ms: int):
    def runner(cmd, **kwargs):
        service = cmd[3]
        for email, token in tokens.items():
            if service == reserve.keychain_service(paths.claude_logins_dir() / email):
                if token == "EXPIRED":
                    blob = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-x", "expiresAt": now_ms - 5}}
                else:
                    blob = {"claudeAiOauth": {"accessToken": token, "expiresAt": now_ms + 60_000}}
                return CompletedProcess(cmd, 0, json.dumps(blob), "")
        return CompletedProcess(cmd, 44, "", "not found")
    return runner


def test_readings_probe_cache_and_pacing(logins, tmp_path, monkeypatch):
    monkeypatch.setattr(reserve, "_last_get_monotonic", None)
    now = reserve.now_local()
    now_ms = int(now.timestamp() * 1000)
    probes = []

    def probe(token):
        probes.append(token)
        return {"a@x-token": _probe(94.0, 49.0), "b@x-token": _probe(70.0, 90.0)}[token]

    sleeps = []
    runner = _token_runner({"a@x": "a@x-token", "b@x": "b@x-token", "c@x": "EXPIRED"}, now_ms)
    cache = tmp_path / "cache.json"
    got = reserve.readings(["a@x", "b@x", "c@x", "d@x"], probe=probe, runner=runner,
                           cache_path=cache, sleep=sleeps.append, now=now)
    assert got["a@x"]["state"] == "reserved" and got["a@x"]["status"] == "ok"
    assert got["b@x"]["state"] == "slack" and got["b@x"]["slack"] == pytest.approx(0.10)
    assert got["c@x"] == {**got["c@x"], "status": "expired-token", "state": "unmeasured"}
    assert got["d@x"]["status"] == "no-login" and got["d@x"]["state"] == "unmeasured"
    assert probes == ["a@x-token", "b@x-token"]
    # Second GET waited for the spacing; expired/no-login lanes never hit the endpoint.
    assert len(sleeps) == 1 and 0 < sleeps[0] <= reserve.DEFAULT_POLICY["usage_spacing_s"]
    # The cache holds numbers and statuses only, never a token.
    text = cache.read_text()
    assert "token" not in text.replace("expired-token", "") and "sk-ant" not in text
    assert json.loads(text)["a@x"]["shared"] == 94.0

    # Within the TTL the ok readings are served from cache; failures re-probe.
    again = reserve.readings(["a@x", "c@x"], probe=probe, runner=runner,
                             cache_path=cache, sleep=sleeps.append, now=now)
    assert probes == ["a@x-token", "b@x-token"]
    assert again["a@x"]["state"] == "reserved" and again["c@x"]["status"] == "expired-token"


def test_readings_map_401_and_429(logins, tmp_path, monkeypatch):
    monkeypatch.setattr(reserve, "_last_get_monotonic", None)
    now = reserve.now_local()
    runner = _token_runner({"a@x": "t-a", "b@x": "t-b"}, int(now.timestamp() * 1000))
    probe = lambda token: {"t-a": {"status": "token-invalid"}, "t-b": {"status": "rate-limited"}}[token]
    got = reserve.readings(["a@x", "b@x"], probe=probe, runner=runner,
                           cache_path=tmp_path / "c.json", sleep=lambda s: None, now=now)
    assert got["a@x"]["status"] == "expired-token" and got["a@x"]["state"] == "unmeasured"
    assert got["b@x"]["status"] == "rate-limited" and got["b@x"]["state"] == "unmeasured"


# --- heal ------------------------------------------------------------------------

def test_heal_runs_detached_as_the_login_and_rate_limits(logins, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "lane-token-must-not-leak")
    monkeypatch.setenv("SUBFLEET_RUN_ID", "r1")
    launched = []

    def popen(cmd, **kwargs):
        launched.append((cmd, kwargs))

    which = lambda name: "/usr/local/bin/claude"
    assert reserve.heal("a@x", popen=popen, which=which, now=1000.0) is True
    cmd, kwargs = launched[0]
    assert cmd[:2] == ["/usr/local/bin/claude", "-p"] and "--model" in cmd and "fable" in cmd
    env = kwargs["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(logins / "a@x")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "SUBFLEET_RUN_ID" not in env
    assert kwargs["start_new_session"] is True
    # Marker written: a second heal inside the interval is refused.
    assert reserve.heal("a@x", popen=popen, which=which, now=time.time()) is False
    assert len(launched) == 1
    # No claude binary or no login dir: nothing launched.
    assert reserve.heal("zz@x", popen=popen, which=which) is False
    assert reserve.heal("b@x", popen=popen, which=lambda n: None) is False


# --- filter ---------------------------------------------------------------------

def _readings_fn(states):
    def fn(emails, pol=None, now=None):
        out = {}
        for email in emails:
            state, status, slack = states.get(email, ("unmeasured", "no-login", None))
            out[email] = {"state": state, "status": status, "slack": slack,
                          "shared": 94.0, "fable": 49.0, "checked_at": "2026-09-08T16:40:00-04:00"}
        return out
    return fn


def test_filter_lanes_keeps_slack_and_no_fable_drops_the_rest_and_heals_expired():
    rows = [{"email": e} for e in ("a@x", "b@x", "c@x", "d@x")]
    healed = []
    kept, dropped = reserve.filter_lanes(
        rows, model="claude-opus-5", pol=dict(reserve.DEFAULT_POLICY),
        readings_fn=_readings_fn({
            "a@x": ("reserved", "ok", -0.96), "b@x": ("slack", "ok", 0.1),
            "c@x": ("unmeasured", "expired-token", None), "d@x": ("no-fable", "ok", None),
        }),
        heal_fn=lambda email, pol=None: healed.append(email) or True,
    )
    assert [r["email"] for r in kept] == ["b@x", "d@x"]
    assert [d["lane"] for d in dropped] == ["a@x", "c@x"]
    assert kept[0]["reserve"]["slack"] == 0.1
    assert healed == ["c@x"]
    text = reserve.describe(dropped)
    assert "a@x slack -0.96" in text and "c@x unmeasured (expired-token)" in text


def test_filter_lanes_passes_fable_and_disabled_policy_through():
    rows = [{"email": "a@x"}]
    boom = lambda *a, **k: pytest.fail("readings must not be taken")
    assert reserve.filter_lanes(rows, model="claude-fable-5-1", readings_fn=boom) == (rows, [])
    off = {**reserve.DEFAULT_POLICY, "enabled": False}
    assert reserve.filter_lanes(rows, model="claude-opus-5", pol=off, readings_fn=boom) == (rows, [])


def test_heals_per_call_is_bounded():
    rows = [{"email": f"{i}@x"} for i in range(6)]
    healed = []
    reserve.filter_lanes(
        rows, model="sonnet", pol=dict(reserve.DEFAULT_POLICY),
        readings_fn=_readings_fn({r["email"]: ("unmeasured", "expired-token", None) for r in rows}),
        heal_fn=lambda email, pol=None: healed.append(email) or True,
    )
    assert len(healed) == reserve.DEFAULT_POLICY["heals_per_call"]


# --- dispatch guard (delegate) ----------------------------------------------------

@pytest.fixture
def guarded(tmp_path, monkeypatch):
    """Delegate with the reserve ON and every lane's reading injected."""
    state = tmp_path / "state"
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"enrolled": {"a@x": "a", "b@x": "b"}}))
    monkeypatch.setenv("DELEGATE_STATE_DIR", str(state))
    monkeypatch.setenv("DELEGATE_ACCOUNTS_FILE", str(accounts))
    monkeypatch.setattr(delegate, "_active_desktop_email", lambda: None)
    paths.reserve_policy_path().write_text(json.dumps({"enabled": True}))
    return state


def _lanes(*emails):
    return [capacity_row("claude", email, score=90) for email in emails]


def _install_readings(monkeypatch, states):
    monkeypatch.setattr(reserve, "readings", _readings_fn(states))
    monkeypatch.setattr(reserve, "heal", lambda email, pol=None: False)


def _cmds(calls):
    return [(cmd[cmd.index("-m") + 1], cmd[cmd.index("-a") + 1]) for cmd in calls if "-m" in cmd]


def test_opus_with_slack_dispatches_on_the_slack_lane(guarded, monkeypatch):
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x", "b@x")))
    _install_readings(monkeypatch, {"a@x": ("reserved", "ok", -0.9), "b@x": ("slack", "ok", 0.12)})
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    assert delegate.main(["--task", "build", "--tier", "standard", "task", "-o", str(guarded / "o.md")]) == 0
    assert _cmds(calls) == [("claude-opus-5", "b@x")]
    record = json.loads((guarded / "decisions.jsonl").read_text().splitlines()[-1])
    assert record["model"] == "opus"
    assert record["reserve"]["action"] == "dispatched"
    assert [k["lane"] for k in record["reserve"]["kept"]] == ["b@x"]
    assert [d["lane"] for d in record["reserve"]["drops"]] == ["a@x"]
    assert "blocked_model" not in record["reserve"]


LANE_ENTRY_KEYS = {"lane", "state", "slack", "shared", "fable", "status", "checked_at"}


def _last_record(state):
    return json.loads((state / "decisions.jsonl").read_text().splitlines()[-1])


def test_dry_run_decision_record_lists_kept_and_dropped_lanes(guarded, monkeypatch, capsys):
    """The 2026-09-18 15:44-16:00 shape: thirteen Opus dispatches landed on the
    one slack lane while another lane was withheld as reserved, and every
    record said ``reserve: null``. A dry run of that dispatch must record both
    lanes, the policy, and the action -- and nothing that looks like a token."""
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x", "b@x")))
    readings = _readings_fn({"a@x": ("reserved", "ok", -0.9), "b@x": ("slack", "ok", 0.12)})

    def leaky_readings(emails, pol=None, now=None):
        # A reading must never be copied wholesale into the record.
        return {email: {**value, "accessToken": "sk-ant-never-logged"}
                for email, value in readings(emails, pol=pol, now=now).items()}
    monkeypatch.setattr(reserve, "readings", leaky_readings)
    monkeypatch.setattr(reserve, "heal", lambda email, pol=None: False)
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([], calls))
    assert delegate.main([
        "--dry-run", "--task", "research", "--tier", "standard", "task",
        "-o", str(guarded / "o.md"),
    ]) == 0
    assert _cmds(calls) == []  # dry run: nothing launched
    printed = capsys.readouterr().out
    assert "-a b@x" in printed and "claude-opus-5" in printed
    line = (guarded / "decisions.jsonl").read_text().splitlines()[-1]
    assert "sk-ant-never-logged" not in line and "accessToken" not in line
    record = json.loads(line)
    assert record["model"] == "opus" and record["lane/home"] == "b@x"
    note = record["reserve"]
    assert note["policy"] == {"enabled": True, "cap_ratio": 2.0, "min_slack": 0.05}
    assert note["action"] == "dispatched"
    assert note["kept"] == [{
        "lane": "b@x", "state": "slack", "slack": 0.12, "shared": 94.0, "fable": 49.0,
        "status": "ok", "checked_at": "2026-09-08T16:40:00-04:00",
    }]
    assert note["drops"] == [{
        "lane": "a@x", "state": "reserved", "slack": -0.9, "shared": 94.0, "fable": 49.0,
        "status": "ok", "checked_at": "2026-09-08T16:40:00-04:00",
    }]
    for entry in (*note["kept"], *note["drops"]):
        assert set(entry) == LANE_ENTRY_KEYS
    assert "blocked_model" not in note


def test_pinned_slack_lane_is_recorded_as_kept(guarded, monkeypatch):
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x", "b@x")))
    _install_readings(monkeypatch, {"a@x": ("reserved", "ok", -0.96), "b@x": ("slack", "ok", 0.2)})
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    assert delegate.main(["-m", "opus", "-a", "b@x", "task", "-o", str(guarded / "o.md")]) == 0
    assert _cmds(calls) == [("claude-opus-5", "b@x")]
    note = _last_record(guarded)["reserve"]
    assert note["action"] == "dispatched"
    assert [k["lane"] for k in note["kept"]] == ["b@x"] and note["kept"][0]["state"] == "slack"
    assert note["drops"] == []  # a pin never consults the other lanes


def test_disabled_policy_records_the_lanes_it_let_through_unread(guarded, monkeypatch):
    paths.reserve_policy_path().write_text(json.dumps({"enabled": False}))
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x", "b@x")))
    monkeypatch.setattr(reserve, "readings", lambda *a, **k: pytest.fail("disabled policy must not read"))
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    assert delegate.main(["-m", "opus", "task", "-o", str(guarded / "o.md")]) == 0
    note = _last_record(guarded)["reserve"]
    assert note["policy"]["enabled"] is False
    assert note["action"] == "dispatched" and note["drops"] == []
    assert [k["lane"] for k in note["kept"]] == ["a@x", "b@x"]
    assert all(k["state"] is None and k["slack"] is None for k in note["kept"])


def test_no_dispatchable_lane_still_records_the_reserve_view(guarded, monkeypatch):
    limited = [capacity_row("claude", "a@x", score=0, dispatchable=False)]
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=limited))
    monkeypatch.setattr(reserve, "heal", lambda email, pol=None: False)
    seen = []

    def readings(emails, pol=None, now=None):
        seen.append(list(emails))
        return _readings_fn({})(emails, pol=pol, now=now)
    monkeypatch.setattr(reserve, "readings", readings)
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([], calls))
    assert delegate.main(["-m", "opus", "task", "-o", str(guarded / "o.md")]) == 3
    assert seen == [[]]  # no candidate reached the reserve
    note = _last_record(guarded)["reserve"]
    assert note == {
        "policy": {"enabled": True, "cap_ratio": 2.0, "min_slack": 0.05},
        "kept": [], "drops": [], "action": "no lane",
    }


def test_opus_blocked_everywhere_upgrades_to_fable_when_codex_is_closed(guarded, monkeypatch, capsys):
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x", "b@x")))
    _install_readings(monkeypatch, {"a@x": ("reserved", "ok", -0.96), "b@x": ("unmeasured", "expired-token", None)})
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    assert delegate.main(["--task", "build", "--tier", "standard", "task", "-o", str(guarded / "o.md")]) == 0
    assert _cmds(calls) == [("claude-fable-5-1", "a@x")]
    err = capsys.readouterr().err
    assert "FABLE RESERVE" in err and "a@x slack -0.96" in err and "b@x unmeasured (expired-token)" in err
    record = json.loads((guarded / "decisions.jsonl").read_text().splitlines()[-1])
    assert record["model"] == "fable"
    assert record["reserve"]["blocked_model"] == "claude-opus-5"
    assert record["reserve"]["action"] == "upgraded to fable"
    assert [d["lane"] for d in record["reserve"]["drops"]] == ["a@x", "b@x"]
    assert record["reserve"]["kept"] == []
    assert {"cap_ratio", "min_slack"} <= set(record["reserve"]["policy"])
    assert record["routing_history"][-1]["to"] == "fable"


def test_opus_blocked_everywhere_upgrades_to_astra_when_codex_has_room(guarded, monkeypatch):
    codex = [capacity_row("codex", "/home/codex-1", score=80)]
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=codex, claude_rows=_lanes("a@x")))
    _install_readings(monkeypatch, {"a@x": ("reserved", "ok", -0.5)})
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    assert delegate.main(["--task", "build", "--tier", "standard", "task", "-o", str(guarded / "o.md")]) == 0
    cmd = calls[-1]
    assert cmd[cmd.index("-m") + 1] == "gpt-6-astra" and cmd[cmd.index("-H") + 1] == "/home/codex-1"
    record = json.loads((guarded / "decisions.jsonl").read_text().splitlines()[-1])
    assert record["reserve"]["action"] == "upgraded to astra" and record["model"] == "astra"


def test_explicit_opus_on_a_pinned_reserved_lane_runs_fable_on_that_lane(guarded, monkeypatch, capsys):
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x", "b@x")))
    _install_readings(monkeypatch, {"a@x": ("reserved", "ok", -0.96), "b@x": ("slack", "ok", 0.2)})
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    assert delegate.main(["-m", "opus", "-a", "a@x", "task", "-o", str(guarded / "o.md")]) == 0
    assert _cmds(calls) == [("claude-fable-5-1", "a@x")]
    assert "FABLE RESERVE" in capsys.readouterr().err


def test_explicit_opus_without_pin_skips_reserved_lanes(guarded, monkeypatch):
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x", "b@x")))
    _install_readings(monkeypatch, {"a@x": ("reserved", "ok", -0.96), "b@x": ("no-fable", "ok", None)})
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    assert delegate.main(["-m", "opus", "task", "-o", str(guarded / "o.md")]) == 0
    assert _cmds(calls) == [("claude-opus-5", "b@x")]


def test_sonnet_and_haiku_tiers_are_guarded_too(guarded, monkeypatch):
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x")))
    _install_readings(monkeypatch, {"a@x": ("reserved", "ok", -1.0)})
    for tier in ("trivial", "easy"):
        calls = []
        monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
        assert delegate.main(["--task", "lookup", "--tier", tier, "task", "-o", str(guarded / f"{tier}.md")]) == 0
        assert _cmds(calls) == [("claude-fable-5-1", "a@x")]


def test_fable_dispatch_never_consults_the_reserve(guarded, monkeypatch):
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x")))
    monkeypatch.setattr(reserve, "readings", lambda *a, **k: pytest.fail("Fable must not read the reserve"))
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    assert delegate.main(["-m", "fable", "task", "-o", str(guarded / "o.md")]) == 0
    assert _cmds(calls) == [("claude-fable-5-1", "a@x")]


def test_policy_file_disabled_restores_the_old_picker(guarded, monkeypatch):
    paths.reserve_policy_path().write_text(json.dumps({"enabled": False}))
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x")))
    monkeypatch.setattr(reserve, "readings", lambda *a, **k: pytest.fail("disabled policy must not read"))
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(0, "ok", "")], calls))
    assert delegate.main(["-m", "opus", "task", "-o", str(guarded / "o.md")]) == 0
    assert _cmds(calls) == [("claude-opus-5", "a@x")]


def test_rc4_retry_does_not_fall_onto_a_reserved_lane(guarded, monkeypatch):
    monkeypatch.setattr(delegate, "_capacity_report",
                        lambda: capacity_snapshot(codex_rows=[], claude_rows=_lanes("a@x", "b@x")))
    _install_readings(monkeypatch, {"a@x": ("slack", "ok", 0.3), "b@x": ("reserved", "ok", -0.5)})
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run",
                        fake_run_factory([(4, "", "usage limit"), (0, "ok", "")], calls))
    assert delegate.main(["-m", "opus", "task", "-o", str(guarded / "o.md")]) == 0
    # First attempt hits a limit on the slack lane; the retry must not use b@x
    # for Opus -- with a@x cooled and b@x reserved the work moves up to Fable.
    assert _cmds(calls) == [("claude-opus-5", "a@x"), ("claude-fable-5-1", "a@x")] or \
        _cmds(calls) == [("claude-opus-5", "a@x"), ("claude-fable-5-1", "b@x")]
    records = [json.loads(line) for line in (guarded / "decisions.jsonl").read_text().splitlines()]
    first, last = records[0], records[-1]
    assert first["model"] == "opus" and first["result"] == 4
    assert first["reserve"]["action"] == "dispatched"
    assert [k["lane"] for k in first["reserve"]["kept"]] == ["a@x"]
    assert [d["lane"] for d in first["reserve"]["drops"]] == ["b@x"]
    assert last["model"] == "fable" and last["reserve"]["action"] == "upgraded to fable"
    assert last["reserve"]["kept"] == [] and [d["lane"] for d in last["reserve"]["drops"]] == ["b@x"]


# --- CLI table ---------------------------------------------------------------------

def test_table_orders_slack_first_and_renders(monkeypatch):
    rows = reserve.table(
        ["a@x", "b@x", "c@x"],
        pol=dict(reserve.DEFAULT_POLICY),
        readings_fn=_readings_fn({"a@x": ("reserved", "ok", -0.9), "b@x": ("slack", "ok", 0.1),
                                  "c@x": ("unmeasured", "no-login", None)}),
    )
    assert [r["account"] for r in rows] == ["b@x", "a@x", "c@x"]
    text = reserve.human_table(rows)
    assert "FABLE RESERVE" in text and "b@x" in text and "+0.10" in text


# --- blind-lane JIT probe falls back to the login-dir reading -----------------------

def test_parse_reading_carries_the_five_hour_window():
    probe = {**_probe(40.0, 80.0), "five_hour": {"used_percent": 10.0, "reset_at": "2026-09-08T22:00:00+00:00"}}
    got = reserve.parse_reading(probe)
    assert got["five_hour"] == 10.0 and got["five_hour_resets_at"].startswith("2026-09-08T22")


def test_headroom_probe_shapes_a_reading_like_the_oauth_probe():
    fn = lambda emails, pol=None, now=None: {"a@x": {
        "status": "ok", "shared": 40.0, "resets_at": "R7", "five_hour": 10.0, "five_hour_resets_at": "R5",
    }}
    got = reserve.headroom_probe("a@x", readings_fn=fn)
    assert got == {"status": "ok", "five_hour": {"used_percent": 10.0, "reset_at": "R5"},
                   "seven_day": {"used_percent": 40.0, "reset_at": "R7"}}
    assert reserve.headroom_probe("a@x", readings_fn=lambda *a, **k: {"a@x": {"status": "expired-token"}}) is None


def test_jit_lane_probe_uses_the_login_reading_when_the_setup_token_is_throttled(monkeypatch):
    monkeypatch.setattr(delegate, "_jit_probe_cache", {})
    monkeypatch.setattr(delegate.claude_side, "roster_config", lambda: {"enrolled": {"a@x": "claude-quota-a@x"}})
    monkeypatch.setattr(delegate.claude_side, "agent_secret_get", lambda name: "setup-token")
    monkeypatch.setattr(delegate.claude_side, "probe_oauth_usage",
                        lambda token, timeout=10.0: {"status": "rate-limited"})
    monkeypatch.setattr(reserve, "readings", lambda emails, pol=None, now=None: {"a@x": {
        "status": "ok", "shared": 40.0, "resets_at": "2026-09-15T05:00:00+00:00",
        "five_hour": 10.0, "five_hour_resets_at": None,
    }})
    got = delegate._probe_lane_headroom("a@x")
    # min headroom across windows: 5h 90, weekly 60 -> 60
    assert got == {"status": "ok", "score": 60.0, "reset_at": "2026-09-15T05:00:00+00:00"}


def test_jit_lane_probe_stays_blind_without_a_login_reading(monkeypatch):
    monkeypatch.setattr(delegate, "_jit_probe_cache", {})
    monkeypatch.setattr(delegate.claude_side, "roster_config", lambda: {"enrolled": {"a@x": "claude-quota-a@x"}})
    monkeypatch.setattr(delegate.claude_side, "agent_secret_get", lambda name: "setup-token")
    monkeypatch.setattr(delegate.claude_side, "probe_oauth_usage",
                        lambda token, timeout=10.0: {"status": "rate-limited"})
    monkeypatch.setattr(reserve, "readings", lambda emails, pol=None, now=None: {"a@x": {"status": "no-login"}})
    assert delegate._probe_lane_headroom("a@x")["score"] is None

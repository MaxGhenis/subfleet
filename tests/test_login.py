import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from carpool import config, login, paths


def _auth(home: Path, account: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"tokens": {"account_id": account}}))


def _fake_codex(tmp_path: Path, url: str = "https://auth.example.test/authorize?x=1") -> Path:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = login ] || exit 9\n"
        f"echo 'Open this URL: {url} to sign in'\n"
        "sleep 1\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    return executable


def test_target_maps_number_and_configured_app_home(tmp_path, monkeypatch):
    app = tmp_path / "desktop-state"
    monkeypatch.setenv("CARPOOL_CODEX_APP_HOME", str(app))

    assert login._target_home("app") == (app, "app")
    home, slot = login._target_home("07")
    assert home == Path.home() / ".codex-7"
    assert slot == "7"


def test_target_maps_number_to_configured_lane_position(tmp_path):
    first = tmp_path / "lane-alpha"
    second = tmp_path / "lane-beta"
    config.save(
        {
            "accounts": [],
            "enrolled": {},
            "codex_homes": [str(first), str(second)],
        }
    )

    assert login._target_home("2") == (second, "2")


def test_target_mixes_named_and_custom_homes_without_aliasing(tmp_path):
    custom = tmp_path / "custom-lane"
    named = tmp_path / ".codex-1"
    config.save(
        {
            "accounts": [],
            "enrolled": {},
            "codex_homes": [str(custom), str(named)],
        }
    )

    assert login._target_home("1") == (named, "1")
    assert login._target_home("2") == (custom, "2")


@pytest.mark.parametrize("target", ["0", "10", "lane", "APP"])
def test_bad_target_is_rejected(target):
    with pytest.raises(SystemExit):
        login._target_home(target)


def test_stages_detached_server_in_configured_state(tmp_path, monkeypatch, capsys):
    state = tmp_path / "runtime"
    monkeypatch.setenv("CARPOOL_STATE_DIR", str(state))
    executable = _fake_codex(tmp_path)

    assert login.codex_login(
        "7", watch=False, open_browser=False, codex_bin=str(executable), timeout_s=3
    ) == 0

    output = capsys.readouterr().out
    assert "https://auth.example.test/authorize?x=1" in output
    assert "lane ~/.codex-7" in output
    assert (Path.home() / ".codex-7").is_dir()
    log = state / "logs" / "codex-login-7.log"
    pid = state / "codex-login-7.pid"
    assert log.is_file() and pid.read_text().strip().isdigit()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(pid.stat().st_mode) == 0o600


def test_missing_url_is_clean_failure(tmp_path, monkeypatch, capsys):
    executable = tmp_path / "silent-codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)

    assert login.codex_login(
        "8", watch=False, open_browser=False, codex_bin=str(executable), timeout_s=1
    ) == 1
    assert "no authorize URL" in capsys.readouterr().err


def test_default_browser_is_used_without_a_fixed_application(monkeypatch):
    seen = []
    monkeypatch.setattr(login.webbrowser, "open", lambda url, new=0: seen.append((url, new)) or True)

    assert login._open_authorize_url("https://auth.example.test/start") is True
    assert seen == [("https://auth.example.test/start", 2)]


def test_configured_browser_is_argv_and_supports_url_placeholder(monkeypatch):
    config.save(
        {
            "accounts": [],
            "enrolled": {},
            "codex_homes": [],
            "browser_cmd": ["browser-tool", "--new-tab", "{url}"],
        }
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    assert login._open_authorize_url("https://auth.example.test/start", runner=run) is True
    assert calls[0][0] == ["browser-tool", "--new-tab", "https://auth.example.test/start"]
    assert calls[0][1]["check"] is False


def test_reports_numbered_duplicates_separately_from_app_shadow(tmp_path, monkeypatch):
    app = tmp_path / "app"
    lane1 = tmp_path / ".codex-1"
    lane2 = tmp_path / ".codex-2"
    lane3 = tmp_path / ".codex-3"
    _auth(app, "account-a")
    _auth(lane1, "account-a")
    _auth(lane2, "account-b")
    _auth(lane3, "account-b")
    monkeypatch.setenv("CARPOOL_CODEX_APP_HOME", str(app))
    config.save(
        {
            "accounts": [],
            "enrolled": {},
            "codex_homes": [str(lane1), str(lane2), str(lane3), str(app)],
            "codex_app_home": str(app),
        }
    )

    assert login.duplicate_lane_report() == [[lane2, lane3]]
    assert login.app_shadow_report() == [lane1]
    assert app not in login.numbered_lane_homes()


def _pending_files(state: Path, slot: str, *, complete: bool) -> tuple[Path, Path]:
    logs = state / "logs"
    logs.mkdir(parents=True)
    log = logs / f"codex-login-{slot}.log"
    log.write_text("Successfully logged in\n" if complete else "waiting\n")
    pid = state / f"codex-login-{slot}.pid"
    pid.write_text("424242\n")
    return log, pid


def test_completion_notifies_shadow_and_runs_configured_refresh(tmp_path, monkeypatch):
    state = paths.state_dir()
    app = tmp_path / "app"
    lane = tmp_path / ".codex-1"
    _auth(app, "account-a")
    _auth(lane, "account-a")
    _pending_files(state, "1", complete=True)
    monkeypatch.setenv("CARPOOL_CODEX_APP_HOME", str(app))
    config.save(
        {
            "accounts": [],
            "enrolled": {},
            "codex_homes": [str(lane)],
            "codex_app_home": str(app),
            "watch_cmd": ["refresh-tool", "--once"],
        }
    )
    messages = []
    commands = []
    monkeypatch.setattr(login.notify, "send", lambda subject, body: messages.append((subject, body)) or True)
    monkeypatch.setattr(
        login.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0),
    )

    assert login.watch_codex_login("1", timeout_s=0) == 0
    assert commands == [["refresh-tool", "--once"]]
    assert len(messages) == 1
    assert "complete" in messages[0][0].lower()
    assert "shadowed" in messages[0][1].lower()
    assert "refresh completed" in messages[0][1].lower()


def test_duplicate_completion_notifies_without_refresh(tmp_path, monkeypatch):
    state = paths.state_dir()
    lane1 = tmp_path / ".codex-1"
    lane2 = tmp_path / ".codex-2"
    _auth(lane1, "account-a")
    _auth(lane2, "account-a")
    _pending_files(state, "1", complete=True)
    config.save(
        {
            "accounts": [],
            "enrolled": {},
            "codex_homes": [str(lane1), str(lane2)],
            "codex_app_home": str(tmp_path / "app"),
        }
    )
    messages = []
    monkeypatch.setattr(login.notify, "send", lambda subject, body: messages.append((subject, body)) or True)
    monkeypatch.setattr(login, "_refresh_snapshot", lambda: pytest.fail("duplicate refreshed"))

    assert login.watch_codex_login("1", timeout_s=0) == 0
    assert "duplicate" in messages[0][0].lower()
    assert "lane 1 + lane 2" in messages[0][1].lower()


def test_dead_login_notifies_unfinished(tmp_path, monkeypatch):
    state = paths.state_dir()
    _pending_files(state, "2", complete=False)
    messages = []
    monkeypatch.setattr(login, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(login.notify, "send", lambda subject, body: messages.append((subject, body)) or True)

    assert login.watch_codex_login("2", timeout_s=30) == 1
    assert "unfinished" in messages[0][0].lower()


def test_timeout_notifies_pending(monkeypatch):
    messages = []
    monkeypatch.setattr(login.notify, "send", lambda subject, body: messages.append((subject, body)) or True)

    assert login.watch_codex_login("app", timeout_s=0) == 1
    assert "timeout" in messages[0][0].lower()


def test_watcher_entrypoint_is_executable_and_repo_relative():
    watcher = Path(__file__).resolve().parents[1] / "bin" / "carpool-login-watch"
    text = watcher.read_text()

    assert os.access(watcher, os.X_OK)
    assert 'ROOT=$(dirname -- "$SCRIPT_DIR")' in text
    assert "-m carpool.login" in text

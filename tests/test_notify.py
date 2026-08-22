import subprocess

from carpool import config, notify


BASE = {"accounts": [], "enrolled": {}, "codex_homes": []}


def test_absent_hook_prints_to_stderr_and_succeeds(capsys):
    config.save(BASE)

    assert notify.send("quota warning", "One lane remains.") is True

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "quota warning" in captured.err
    assert "One lane remains." in captured.err


def test_configured_hook_receives_complete_message(tmp_path, monkeypatch):
    helper = tmp_path / "notify-helper"
    config.save({**BASE, "notify_cmd": [str(helper), "--channel", "ops"]})
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notify.subprocess, "run", run)

    assert notify.send("quota warning", "One lane remains.") is True
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[:3] == [str(helper), "--channel", "ops"]
    delivered = " ".join(cmd[3:]) + str(kwargs.get("input") or "")
    assert "quota warning" in delivered
    assert "One lane remains." in delivered
    assert kwargs.get("shell") is not True


def test_dry_run_does_not_invoke_hook(tmp_path, monkeypatch, capsys):
    config.save({**BASE, "notify_cmd": str(tmp_path / "notify-helper")})

    def fail(*args, **kwargs):
        raise AssertionError("dry-run invoked notify command")

    monkeypatch.setattr(notify.subprocess, "run", fail)
    assert notify.send("quota warning", "One lane remains.", dry_run=True) is True
    captured = capsys.readouterr()
    assert "dry-run" in captured.err
    assert "quota warning" in captured.err


def test_failed_hook_reports_error(tmp_path, monkeypatch, capsys):
    helper = tmp_path / "notify-helper"
    config.save({**BASE, "notify_cmd": str(helper)})

    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 9, "", "transport unavailable")

    monkeypatch.setattr(notify.subprocess, "run", run)
    assert notify.send("quota warning", "One lane remains.") is False
    captured = capsys.readouterr()
    assert "9" in captured.err
    assert "transport unavailable" in captured.err


def test_invalid_hook_configuration_fails_closed(monkeypatch, capsys):
    config.save({**BASE, "notify_cmd": {"executable": "notify-helper"}})

    def fail(*args, **kwargs):
        raise AssertionError("invalid config invoked a command")

    monkeypatch.setattr(notify.subprocess, "run", fail)
    assert notify.send("quota warning", "One lane remains.") is False
    assert "configuration error" in capsys.readouterr().err

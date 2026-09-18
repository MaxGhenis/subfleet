"""paths.claude_bin(): which Claude Code launcher lanes/probes/revives run."""

from __future__ import annotations

import os

from subfleet import paths


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_claude_bin_prefers_override_then_native_launcher_then_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "/explicit/claude")
    assert paths.claude_bin() == "/explicit/claude"

    monkeypatch.delenv("CLAUDE_LANE_CLAUDE")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    brew = _executable(tmp_path / "brew" / "claude")
    monkeypatch.setenv("PATH", str(brew.parent))
    # PATH order alone would pick the (stale) cask; the native launcher wins
    native = _executable(tmp_path / "home" / ".local" / "bin" / "claude")
    assert paths.claude_bin() == str(native)

    native.unlink()
    assert paths.claude_bin() == str(brew)

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert paths.claude_bin() == "claude"

"""Claude-session context handoff to detached Subfleet lanes."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from subfleet import cli, handoff


SESSION = "11111111-2222-4333-8444-555555555555"
OTHER_SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _entry(
    kind: str,
    content,
    *,
    row_uuid: str,
    timestamp: str = "2026-08-28T10:00:00Z",
    cwd: str | None = None,
    **extra,
) -> dict:
    row = {
        "type": kind,
        "uuid": row_uuid,
        "timestamp": timestamp,
        "message": {"role": kind, "content": content},
        "isSidechain": False,
    }
    if cwd is not None:
        row["cwd"] = cwd
    row.update(extra)
    return row


def _transcript(claude_dir: Path, session: str, rows: list[dict]) -> Path:
    path = claude_dir / "projects" / "-Users-max-work" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(path: Path) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "handoff@example.com")
    _git(path, "config", "user.name", "Handoff Tests")
    (path / "tracked.txt").write_text("base\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial handoff state")
    return path, _git(path, "rev-parse", "HEAD")


def test_run_builds_private_detached_prompt_with_safe_tool_context(
    tmp_path, monkeypatch
):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    workdir = tmp_path / "work"
    workdir.mkdir()
    transcript = _transcript(
        claude_dir,
        SESSION,
        [
            _entry("user", "Implement the parser and tests.", row_uuid="u1", cwd=str(workdir)),
            _entry(
                "assistant",
                [
                    {"type": "text", "text": "I found the parser seam."},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "pytest -q tests/test_parser.py"},
                    },
                ],
                row_uuid="a1",
                timestamp="2026-08-28T10:01:00Z",
                cwd=str(workdir),
            ),
            _entry(
                "user",
                [{"type": "tool_result", "tool_use_id": "tool-1", "content": "12 passed in 0.4s"}],
                row_uuid="u2",
                timestamp="2026-08-28T10:02:00Z",
                cwd=str(workdir),
            ),
            _entry(
                "assistant",
                [{"type": "text", "text": "Next I need to wire the CLI."}],
                row_uuid="a2",
                timestamp="2026-08-28T10:03:00Z",
                cwd=str(workdir),
            ),
        ],
    )
    seen = {}

    def fake_delegate(argv):
        prompt = Path(argv[argv.index("-p") + 1])
        seen.update(
            argv=list(argv),
            path=prompt,
            mode=stat.S_IMODE(prompt.stat().st_mode),
            brief=prompt.read_text(),
        )
        return 7

    rc = handoff.run(SESSION, False, "astra", None, delegate_main=fake_delegate)
    assert rc == 7
    assert seen["mode"] == 0o600
    assert not seen["path"].exists()
    assert seen["argv"][:6] == ["-d", "-m", "astra", "-t", "build", "-C"]
    assert seen["argv"][seen["argv"].index("-C") + 1] == str(workdir)
    assert seen["argv"][seen["argv"].index("-n") + 1] == "handoff-11111111"
    assert f"Source transcript: {transcript}" in seen["brief"]
    assert "I found the parser seam." in seen["brief"]
    assert "Claude tool call (Bash):" in seen["brief"]
    assert "12 passed in 0.4s" in seen["brief"]
    assert "pytest -q tests/test_parser.py" in seen["brief"]
    assert seen["brief"].count("Implement the parser and tests.") == 1


def test_tool_results_scrub_credentials_base64_and_sensitive_reads(tmp_path):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    workdir = tmp_path / "work"
    workdir.mkdir()
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    command_secret = "sk-proj-commandsecretabcdefghijklmnopqrstuvwxyz"
    opaque = "opaque-value-that-has-no-recognizable-prefix"
    encoded = "A" * 300
    transcript = _transcript(
        claude_dir,
        SESSION,
        [
            _entry(
                "user",
                "Fix auth parsing; keep the ordinary token budget note.",
                row_uuid="u1",
                cwd=str(workdir),
            ),
            _entry(
                "assistant",
                [{
                    "type": "tool_use",
                    "id": "normal",
                    "name": "Bash",
                    "input": {"command": f"OPENAI_API_KEY={command_secret} pytest -q"},
                }],
                row_uuid="a1",
                cwd=str(workdir),
            ),
            _entry(
                "user",
                [{
                    "type": "tool_result",
                    "tool_use_id": "normal",
                    "content": f"compile ok\nGITHUB_TOKEN={secret}\nblob={encoded}",
                }],
                row_uuid="u2",
                cwd=str(workdir),
            ),
            _entry(
                "assistant",
                [{
                    "type": "tool_use",
                    "id": "sensitive",
                    "name": "Bash",
                    "input": {"command": "agent-secret get agent/github-token"},
                }],
                row_uuid="a2",
                cwd=str(workdir),
            ),
            _entry(
                "user",
                [{
                    "type": "tool_result",
                    "tool_use_id": "sensitive",
                    "content": opaque,
                }],
                row_uuid="u3",
                cwd=str(workdir),
            ),
        ],
    )
    brief, _original = handoff._build_brief(SESSION, transcript, workdir, str(workdir))
    assert "ordinary token budget" in brief
    assert "compile ok" in brief
    assert secret not in brief and command_secret not in brief and encoded not in brief
    assert "OPENAI_API_KEY=[REDACTED] pytest -q" in brief
    assert "GITHUB_TOKEN=[REDACTED]" in brief
    assert "[BASE64 OMITTED]" in brief
    assert opaque not in brief
    assert handoff.OMITTED_SENSITIVE in brief
    assert "agent-secret get" not in brief


def test_recent_excerpt_ignores_meta_sidechain_thinking_and_keeps_task_notifications(
    tmp_path,
):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    workdir = tmp_path / "work"
    workdir.mkdir()
    transcript = _transcript(
        claude_dir,
        SESSION,
        [
            _entry("user", "Implement it.", row_uuid="u1", cwd=str(workdir)),
            _entry(
                "assistant",
                [{"type": "thinking", "thinking": "hidden chain of thought"}],
                row_uuid="a-thinking",
                cwd=str(workdir),
            ),
            _entry(
                "assistant",
                [{"type": "text", "text": "sidechain secret"}],
                row_uuid="side",
                cwd=str(workdir),
                isSidechain=True,
            ),
            _entry(
                "user",
                "meta control message",
                row_uuid="meta",
                cwd=str(workdir),
                isMeta=True,
            ),
            _entry(
                "user",
                "Subagent reports that the focused tests pass.",
                row_uuid="notification",
                cwd=str(workdir),
                origin={"kind": "task-notification"},
            ),
            _entry(
                "assistant",
                [{"type": "text", "text": "Continue with integration."}],
                row_uuid="a2",
                cwd=str(workdir),
            ),
        ],
    )
    with transcript.open("a") as stream:
        stream.write("not-json\n{\"type\":\"assistant\"")
    brief, _original = handoff._build_brief(SESSION, transcript, workdir, str(workdir))
    assert "Subagent reports that the focused tests pass." in brief
    assert "Continue with integration." in brief
    assert "hidden chain of thought" not in brief
    assert "sidechain secret" not in brief
    assert "meta control message" not in brief


def test_last_uses_event_timestamp_not_mtime_and_current_session_wins(
    tmp_path, monkeypatch
):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    workdir = tmp_path / "work"
    workdir.mkdir()
    old = _transcript(
        claude_dir,
        SESSION,
        [_entry("user", "old", row_uuid="u1", timestamp="2026-08-20T10:00:00Z", cwd=str(workdir))],
    )
    new = _transcript(
        claude_dir,
        OTHER_SESSION,
        [_entry("user", "new", row_uuid="u2", timestamp="2026-08-28T10:00:00Z", cwd=str(workdir))],
    )
    os.utime(old, (2_000_000_000, 2_000_000_000))
    os.utime(new, (1_000_000_000, 1_000_000_000))

    resolved, path = handoff._resolve_source(None, True)
    assert (resolved, path) == (OTHER_SESSION, new)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    resolved, path = handoff._resolve_source(None, True)
    assert (resolved, path) == (SESSION, old)


def test_progress_git_status_log_and_salvage_refs_are_included_and_scrubbed(
    tmp_path,
):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    repo, head = _repo(tmp_path / "repo")
    (repo / "PROGRESS.md").write_text(
        "state: parser implemented\nAPI_TOKEN=sk-proj-abcdefghijklmnopqrstuvwxyz\nnext: wire CLI\n"
    )
    (repo / "untracked.py").write_text("pass\n")
    _git(repo, "update-ref", "refs/codex-salvage/handoff-test", head)
    transcript = _transcript(
        claude_dir,
        SESSION,
        [_entry("user", "Implement it.", row_uuid="u1", cwd=str(repo))],
    )
    brief, _original = handoff._build_brief(SESSION, transcript, repo, str(repo))
    assert "state: parser implemented" in brief and "next: wire CLI" in brief
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in brief
    assert "API_TOKEN=[REDACTED]" in brief
    assert "untracked.py" in brief
    assert "initial handoff state" in brief
    assert "refs/codex-salvage/handoff-test" in brief


def test_workdir_override_wins_over_transcript_cwd(tmp_path):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    transcript = _transcript(
        claude_dir,
        SESSION,
        [_entry("user", "Review it.", row_uuid="u1", cwd=str(source))],
    )
    chosen, source_cwd = handoff._workdir(transcript, target)
    assert chosen == target.resolve()
    assert source_cwd == str(source)


def test_scrubber_preserves_ordinary_technical_context():
    source = (
        "token budget is 4096; commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef; "
        "password validation is implemented"
    )
    cleaned, count = handoff.scrub_secrets(source)
    assert (cleaned, count) == (source, 0)

    usage = '{"input_tokens": 1200, "output_tokens": 80, "max_tokens": 4096}'
    assert handoff.scrub_secrets(usage) == (usage, 0)

    secret_text = (
        '"refresh_token": "refresh-value-123"\n'
        "AWS_SECRET_ACCESS_KEY=aws-secret-value\n"
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
        "https://user:password@example.com/repo"
    )
    cleaned, count = handoff.scrub_secrets(secret_text)
    assert count >= 3
    assert "refresh-value-123" not in cleaned
    assert "aws-secret-value" not in cleaned
    assert "abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "user:password@" not in cleaned
    assert handoff._sensitive_tool_call("Bash", {"command": "env"}) is True
    assert handoff._sensitive_tool_call("Read", {"file_path": "/tmp/.env.local"}) is True
    assert handoff._sensitive_tool_call("Bash", {"command": "pytest -q"}) is False


def test_huge_text_tool_result_is_bounded_but_keeps_head_and_tail(tmp_path):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    workdir = tmp_path / "work"
    workdir.mkdir()
    result = "HEAD useful diagnostic\n" + ("ordinary compiler detail\n" * 1_000) + "TAIL final failure"
    transcript = _transcript(
        claude_dir,
        SESSION,
        [
            _entry("user", "Fix the compiler failure.", row_uuid="u1", cwd=str(workdir)),
            _entry(
                "assistant",
                [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make"}}],
                row_uuid="a1",
                cwd=str(workdir),
            ),
            _entry(
                "user",
                [{"type": "tool_result", "tool_use_id": "t1", "content": result}],
                row_uuid="u2",
                cwd=str(workdir),
            ),
        ],
    )
    brief, _original = handoff._build_brief(SESSION, transcript, workdir, str(workdir))
    assert "HEAD useful diagnostic" in brief
    assert "TAIL final failure" in brief
    assert "characters omitted" in brief
    assert len(brief) < 80_000


def test_unmatched_tool_result_at_excerpt_boundary_is_omitted(tmp_path):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    workdir = tmp_path / "work"
    workdir.mkdir()
    opaque_secret = "opaque-secret-with-no-recognizable-prefix"
    rows = [
        _entry("user", "Continue the work.", row_uuid="original", cwd=str(workdir)),
        _entry(
            "assistant",
            [{
                "type": "tool_use",
                "id": "boundary-secret",
                "name": "Bash",
                "input": {"command": "agent-secret get agent/github-token"},
            }],
            row_uuid="sensitive-call",
            cwd=str(workdir),
        ),
        _entry(
            "user",
            [{
                "type": "tool_result",
                "tool_use_id": "boundary-secret",
                "content": opaque_secret,
            }],
            row_uuid="boundary-result",
            cwd=str(workdir),
        ),
    ]
    rows.extend(
        _entry(
            "assistant",
            f"ordinary follow-up {index}",
            row_uuid=f"follow-up-{index}",
            cwd=str(workdir),
        )
        for index in range(handoff.RECENT_RECORDS - 1)
    )
    transcript = _transcript(claude_dir, SESSION, rows)

    brief, _original = handoff._build_brief(SESSION, transcript, workdir, str(workdir))

    assert opaque_secret not in brief
    assert handoff.OMITTED_UNMATCHED in brief


def test_invalid_selection_target_and_missing_transcript_fail_without_dispatch(capsys):
    calls = []

    def never(argv):
        calls.append(argv)
        return 0

    assert handoff.run(None, False, "sol", None, delegate_main=never) == 2
    assert handoff.run(SESSION, False, "fable", None, delegate_main=never) == 2
    assert handoff.run("not-a-uuid", False, "sol", None, delegate_main=never) == 2
    assert handoff.run(SESSION, False, "sol", None, delegate_main=never) == 2
    assert calls == []
    assert "subfleet handoff:" in capsys.readouterr().err


def test_handoff_cli_routes_without_falling_back_to_status(monkeypatch):
    seen = {}

    def fake_run(session_id, *, last, target, workdir):
        seen.update(
            session_id=session_id,
            last=last,
            target=target,
            workdir=workdir,
        )
        return 19

    monkeypatch.setattr(handoff, "run", fake_run)

    assert cli.main(["handoff", SESSION, "--to", "terra", "-C", "/tmp/work"]) == 19
    assert seen == {
        "session_id": SESSION,
        "last": False,
        "target": "terra",
        "workdir": "/tmp/work",
    }


def test_astra_is_a_handoff_target(monkeypatch):
    assert "astra" in handoff.TARGETS
    seen = {}

    def fake_run(session_id, *, last, target, workdir):
        seen.update(target=target, workdir=workdir)
        return 0

    monkeypatch.setattr(handoff, "run", fake_run)
    assert cli.main(["handoff", SESSION, "--to", "astra", "-C", "/tmp/work"]) == 0
    assert seen == {"target": "astra", "workdir": "/tmp/work"}


def test_retired_sol_handoff_target_dispatches_astra(tmp_path, monkeypatch, capsys):
    def stop(*_args):
        raise handoff.HandoffError("stop here")

    monkeypatch.setattr(handoff, "_resolve_source", stop)
    assert handoff.run(SESSION, False, "sol", None, delegate_main=lambda argv: 0) == 2
    err = capsys.readouterr().err
    assert "sol is retired from dispatch" in err
    assert "stop here" in err

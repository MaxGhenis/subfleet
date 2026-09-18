"""Bounded, credential-scrubbed handoffs from Claude sessions to a new lane.

The source transcript remains the durable record. A handoff prompt contains a
bounded main-chain excerpt plus workspace state; it never serializes raw Claude
message objects. Ordinary tool inputs and textual results are useful context, so
we retain bounded excerpts after suppressing credential-reading calls and
removing credential/binary material.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import delegate, notify, paths, tickle
from .util import parse_iso


TARGETS = {"sol", "terra", "astra", "luna", "opus"}
RECENT_RECORDS = 40
RECENT_CHARS = 48_000
TOOL_RESULT_CHARS = 5_000
TOOL_RESULTS_TOTAL_CHARS = 16_000
TOOL_INPUT_CHARS = 4_000
TOOL_INPUTS_TOTAL_CHARS = 12_000
ORIGINAL_TASK_CHARS = 24_000
PROGRESS_CHARS = 32_000
REPO_SECTION_CHARS = 16_000
MAX_JSON_LINE_CHARS = 4 * 1024 * 1024
LAST_SCAN_BYTES = 2 * 1024 * 1024
FULL_SCAN_BYTES = 64 * 1024 * 1024
REDACTED = "[REDACTED]"
OMITTED_BINARY = "[binary/base64 tool result omitted]"
OMITTED_SENSITIVE = "[credential-reading tool result omitted]"
OMITTED_UNMATCHED = "[tool result omitted because its input is outside this excerpt]"


class HandoffError(ValueError):
    """A user-facing handoff selection or source error."""


_PEM_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_PREFIXED_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"glpat-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|"
    r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"npm_[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{16,}"
    r")(?![A-Za-z0-9_-])"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*")
_DATA_URI_RE = re.compile(
    r"(?i)data:[a-z0-9.+/-]+(?:;[a-z0-9=.+/-]+)*;base64,[A-Za-z0-9+/=\s]{32,}"
)
_LONG_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{160,}={0,2})(?![A-Za-z0-9+/])"
)
_URL_PASSWORD_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)"
)
_HEADER_RE = re.compile(r"(?im)^(\s*(?:authorization|cookie|set-cookie)\s*:\s*).+$")
_SENSITIVE_KEY = (
    r"(?:(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|"
    r"credential|credentials|private[_-]?key|signing[_-]?key|"
    r"secret[_-]?access[_-]?key|access[_-]?key[_-]?id|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|oauth[_-]?token|auth[_-]?token)|"
    r"(?:[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*)[_-](?:api[_-]?key|token|secret|"
    r"password|passwd|private[_-]?key|signing[_-]?key))"
)
_QUOTED_ASSIGN_RE = re.compile(
    rf"(?im)(?P<prefix>[\"']?{_SENSITIVE_KEY}[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>[^\r\n]*?)(?P=quote)"
)
_PLAIN_ASSIGN_RE = re.compile(
    rf"(?im)(?P<prefix>[\"']?{_SENSITIVE_KEY}[\"']?\s*[:=]\s*)"
    r"(?P<value>[^\s,;\"']+)"
)
_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE
)
_SENSITIVE_TOOL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\bagent-secret\s+(?:get|show)\b",
        r"\bsecurity\s+(?:dump-keychain|find-generic-password\b.*(?:\s-w\b|--password\b))",
        r"(?:^|[;&|]\s*|\bsudo\s+|[\"']command[\"']\s*:\s*[\"'])"
        r"(?:env|printenv)(?:\s|[\"']|$)",
        r"(?:auth\.json|credentials(?:\.json)?|(?:^|[/\s])\.env"
        r"(?:\.[A-Za-z0-9_-]+)?(?=[\s\"']|$))",
    )
)


def _replace_with_count(
    pattern: re.Pattern[str], text: str, replacement: str | Callable[[re.Match[str]], str]
) -> tuple[str, int]:
    return pattern.subn(replacement, text)


def scrub_secrets(text: str) -> tuple[str, int]:
    """Remove credential values and encoded binary while retaining ordinary text."""
    total = 0
    for pattern, replacement in (
        (_PEM_RE, "[PRIVATE KEY REDACTED]"),
        (_DATA_URI_RE, "[BASE64 DATA OMITTED]"),
        (_JWT_RE, REDACTED),
        (_PREFIXED_TOKEN_RE, REDACTED),
        (_BEARER_RE, "Bearer " + REDACTED),
        (_LONG_BASE64_RE, "[BASE64 OMITTED]"),
        (_HEADER_RE, lambda match: match.group(1) + REDACTED),
        (
            _URL_PASSWORD_RE,
            lambda match: match.group(1) + REDACTED + match.group(3),
        ),
        (
            _QUOTED_ASSIGN_RE,
            lambda match: (
                match.group("prefix")
                + match.group("quote")
                + REDACTED
                + match.group("quote")
            ),
        ),
        (_PLAIN_ASSIGN_RE, lambda match: match.group("prefix") + REDACTED),
    ):
        text, count = _replace_with_count(pattern, text, replacement)
        total += count
    return text, total


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    marker = f"\n… [{len(text) - limit:,} characters omitted] …\n"
    usable = max(0, limit - len(marker))
    head = int(usable * 0.6)
    return text[:head].rstrip() + marker + text[-(usable - head) :].lstrip()


def _clean_text(text: str, limit: int) -> tuple[str, int]:
    text = _SYSTEM_REMINDER_RE.sub("", text)
    text, redactions = scrub_secrets(text)
    return _truncate(text, limit), redactions


def _entry_is_main(entry: Any) -> bool:
    return bool(
        isinstance(entry, dict)
        and entry.get("type") in {"user", "assistant"}
        and not entry.get("isSidechain")
        and not entry.get("isMeta")
    )


def _parse_line(line: str) -> dict[str, Any] | None:
    if len(line) > MAX_JSON_LINE_CHARS:
        return None
    try:
        value = json.loads(line)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _content_blocks(message: Any) -> list[dict[str, Any]]:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _ordinary_text(blocks: Iterable[dict[str, Any]]) -> str:
    return "\n".join(
        str(block.get("text") or "")
        for block in blocks
        if block.get("type") == "text"
    ).strip()


def _synthetic_text(text: str) -> bool:
    stripped = text.strip()
    return bool(
        stripped == tickle.RESUME_STUB_USER
        or stripped == tickle.RESUME_STUB_ASSISTANT
        or stripped.startswith("[Request interrupted by user")
        or tickle.MARKER in stripped[:400]
        or tickle.MUSTER_MARKER in stripped[:400]
    )


def _first_task(path: Path) -> tuple[str, str | None, int]:
    try:
        stream = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HandoffError(f"cannot read transcript {path}: {exc}") from exc
    with stream:
        for line in stream:
            entry = _parse_line(line)
            if not _entry_is_main(entry) or entry.get("type") != "user":
                continue
            origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
            if origin.get("kind") in {"task-notification", "peer"}:
                continue
            text = _ordinary_text(_content_blocks(entry.get("message")))
            if not text or _synthetic_text(text):
                continue
            cleaned, redactions = _clean_text(text, ORIGINAL_TASK_CHARS)
            if cleaned:
                return cleaned, entry.get("uuid"), redactions
    raise HandoffError(f"no user task text found in transcript {path}")


def _reverse_main_entries(
    path: Path, *, limit: int = RECENT_RECORDS, max_bytes: int = FULL_SCAN_BYTES
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in tickle._lines_reversed(path, max_bytes=max_bytes):
        entry = _parse_line(line)
        if not _entry_is_main(entry):
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break
    entries.reverse()
    return entries


def _sensitive_tool_call(name: str, value: Any) -> bool:
    if "agent-secret" in name.casefold() or "keychain" in name.casefold():
        return True
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return any(pattern.search(rendered) for pattern in _SENSITIVE_TOOL_PATTERNS)


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "") if content.get("type") == "text" else ""
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
    return ""


def _tool_input_text(name: str, value: Any) -> str:
    """Readable input context without committing to any provider's tool schema."""
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and (
            name.casefold() in {"bash", "shell", "shell_command"}
            or "exec" in name.casefold()
        ):
            return command
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _looks_binary(text: str) -> bool:
    if "\x00" in text:
        return True
    sample = text[:16_384]
    if not sample:
        return False
    printable = sum(character.isprintable() or character in "\r\n\t" for character in sample)
    return printable / len(sample) < 0.85


def _recent_excerpt(path: Path, first_uuid: str | None) -> tuple[str, int]:
    entries = _reverse_main_entries(path)
    tools: dict[str, tuple[str, bool]] = {}
    segments: list[tuple[str, int, str | None]] = []

    def add(
        label: str,
        body: str,
        *,
        tool_kind: str | None = None,
        limit: int = 8_000,
    ) -> None:
        if not body or _synthetic_text(body):
            return
        cleaned, redactions = _clean_text(body, limit)
        if cleaned:
            segments.append((f"{label}\n{cleaned}", redactions, tool_kind))

    for entry in entries:
        role = "Claude user:" if entry.get("type") == "user" else "Claude assistant:"
        blocks = _content_blocks(entry.get("message"))
        text = _ordinary_text(blocks)
        if text and entry.get("uuid") != first_uuid:
            add(role, text)
        for block in blocks:
            kind = block.get("type")
            if kind == "tool_use":
                tool_id = str(block.get("id") or "")
                name = str(block.get("name") or "tool")
                sensitive = _sensitive_tool_call(name, block.get("input"))
                tools[tool_id] = (name, sensitive)
                if sensitive:
                    segments.append(
                        (f"Claude tool call ({name}):\n[credential-reading input omitted]", 1, "input")
                    )
                else:
                    add(
                        f"Claude tool call ({name}):",
                        _tool_input_text(name, block.get("input")),
                        tool_kind="input",
                        limit=TOOL_INPUT_CHARS,
                    )
            elif kind == "tool_result":
                tool_id = str(block.get("tool_use_id") or "")
                matched = tools.get(tool_id)
                if matched is None:
                    segments.append(
                        (f"Claude tool result:\n{OMITTED_UNMATCHED}", 1, "result")
                    )
                    continue
                name, sensitive = matched
                if sensitive:
                    segments.append((f"Claude tool result ({name}):\n{OMITTED_SENSITIVE}", 1, "result"))
                    continue
                result = _tool_result_text(block)
                if not result:
                    continue
                if _looks_binary(result):
                    segments.append((f"Claude tool result ({name}):\n{OMITTED_BINARY}", 1, "result"))
                    continue
                add(
                    f"Claude tool result ({name}):",
                    result,
                    tool_kind="result",
                    limit=TOOL_RESULT_CHARS,
                )

    chosen: list[tuple[str, int, str | None]] = []
    remaining = RECENT_CHARS
    input_remaining = TOOL_INPUTS_TOTAL_CHARS
    result_remaining = TOOL_RESULTS_TOTAL_CHARS
    for text, redactions, tool_kind in reversed(segments):
        if tool_kind == "input":
            allowance = min(remaining, input_remaining)
        elif tool_kind == "result":
            allowance = min(remaining, result_remaining)
        else:
            allowance = remaining
        if allowance <= 0:
            continue
        selected = _truncate(text, allowance)
        if not selected:
            continue
        chosen.append((selected, redactions, tool_kind))
        consumed = len(selected)
        remaining -= consumed
        if tool_kind == "input":
            input_remaining -= consumed
        elif tool_kind == "result":
            result_remaining -= consumed
        if remaining <= 0:
            break
    chosen.reverse()
    return "\n\n".join(item[0] for item in chosen), sum(item[1] for item in chosen)


def _latest_main_metadata(path: Path, *, max_bytes: int = LAST_SCAN_BYTES) -> tuple[datetime | None, str | None]:
    latest_timestamp: datetime | None = None
    latest_cwd: str | None = None
    for line in tickle._lines_reversed(path, chunk=64 * 1024, max_bytes=max_bytes):
        entry = _parse_line(line)
        if not _entry_is_main(entry):
            continue
        if latest_cwd is None and isinstance(entry.get("cwd"), str) and entry.get("cwd"):
            latest_cwd = entry["cwd"]
        if latest_timestamp is None and isinstance(entry.get("timestamp"), str):
            latest_timestamp = parse_iso(entry["timestamp"])
        if latest_timestamp is not None and latest_cwd is not None:
            break
    return latest_timestamp, latest_cwd


def _candidate_transcripts() -> list[Path]:
    projects = paths.claude_dir() / "projects"
    candidates: dict[str, Path] = {}
    for pattern in ("*.jsonl", "*/*.jsonl"):
        try:
            found = projects.glob(pattern)
            for path in found:
                if path.is_file():
                    candidates[str(path)] = path
        except OSError:
            continue
    return list(candidates.values())


def _canonical_session_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise HandoffError(f"invalid Claude session id: {value!r}") from exc
    canonical = str(parsed)
    if value.casefold() != canonical:
        raise HandoffError(f"Claude session id must be a canonical UUID: {value!r}")
    return canonical


def _resolve_source(session_id: str | None, last: bool) -> tuple[str, Path]:
    if bool(session_id) == bool(last):
        raise HandoffError("provide exactly one of SESSION_ID or --last")
    if session_id:
        canonical = _canonical_session_id(session_id)
        transcript = notify.transcript_path(canonical)
        if transcript is None:
            raise HandoffError(f"transcript not found for Claude session {canonical}")
        return canonical, transcript

    current = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if current:
        try:
            canonical = _canonical_session_id(current)
        except HandoffError:
            canonical = ""
        if canonical and (transcript := notify.transcript_path(canonical)) is not None:
            return canonical, transcript

    ranked: list[tuple[tuple[int, float, float, str], str, Path]] = []
    for transcript in _candidate_transcripts():
        try:
            canonical = _canonical_session_id(transcript.stem)
            event_time, _cwd = _latest_main_metadata(transcript)
            mtime = transcript.stat().st_mtime
        except (HandoffError, OSError):
            continue
        key = (
            1 if event_time is not None else 0,
            event_time.timestamp() if event_time is not None else float("-inf"),
            mtime,
            str(transcript),
        )
        ranked.append((key, canonical, transcript))
    if not ranked:
        raise HandoffError("no Claude session transcript found for --last")
    _key, canonical, transcript = max(ranked, key=lambda item: item[0])
    return canonical, transcript


def _read_bounded(path: Path, max_bytes: int = 128 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size <= max_bytes:
                raw = stream.read()
            else:
                half = max_bytes // 2
                raw = stream.read(half)
                stream.seek(max(0, size - half))
                raw += b"\n... [middle omitted] ...\n" + stream.read(half)
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _repository_context(cwd: Path) -> tuple[str, int]:
    probe = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if probe is None or probe.returncode != 0:
        return "Not a Git worktree.", 0

    sections = []
    for title, args, empty in (
        ("Status", ["status", "--short", "--branch", "--untracked-files=normal"], "Clean."),
        ("Recent commits", ["log", "-5", "--oneline", "--decorate"], "No commits."),
        (
            "Salvage refs",
            [
                "for-each-ref",
                "--sort=-creatordate",
                "--count=12",
                "--format=%(refname) %(objectname:short)",
                "refs/codex-salvage",
                "refs/claude-salvage",
            ],
            "None.",
        ),
    ):
        completed = _run_git(cwd, args)
        body = completed.stdout.strip() if completed is not None and completed.returncode == 0 else ""
        sections.append(f"### {title}\n{body or empty}")
    return _clean_text("\n\n".join(sections), REPO_SECTION_CHARS)


def _workdir(transcript: Path, override: str | Path | None) -> tuple[Path, str | None]:
    _stamp, source_cwd = _latest_main_metadata(transcript, max_bytes=FULL_SCAN_BYTES)
    chosen = Path(override).expanduser() if override is not None else (
        Path(source_cwd).expanduser() if source_cwd else None
    )
    if chosen is None:
        raise HandoffError("source transcript has no cwd; pass -C DIR")
    try:
        chosen = chosen.resolve()
    except OSError as exc:
        raise HandoffError(f"cannot resolve workdir {chosen}: {exc}") from exc
    if not chosen.is_dir():
        raise HandoffError(f"workdir is not a directory: {chosen}")
    return chosen, source_cwd


def _build_brief(session_id: str, transcript: Path, cwd: Path, source_cwd: str | None) -> tuple[str, str]:
    original, first_uuid, redactions = _first_task(transcript)
    recent, recent_redactions = _recent_excerpt(transcript, first_uuid)
    redactions += recent_redactions

    progress_path = cwd / "PROGRESS.md"
    if progress_path.is_file():
        progress, count = _clean_text(_read_bounded(progress_path), PROGRESS_CHARS)
        redactions += count
    else:
        progress = "Not present."

    repository, count = _repository_context(cwd)
    redactions += count
    brief = f"""# Cross-agent handoff

Continue the source session's work in the target worktree. Inspect the actual
workspace before acting: this is a bounded excerpt, not an authoritative state
snapshot. Ordinary tool inputs and textual results were bounded and credential-
scrubbed; thinking, binary payloads, and credential-reading inputs/results were
omitted. The full transcript may contain sensitive raw material; consult it only
when necessary and never expose credentials.

- Source provider: Claude Code
- Source session: {session_id}
- Source transcript: {transcript}
- Source cwd: {source_cwd or "unknown"}
- Target cwd: {cwd}
- Credential/binary redactions in this brief: {redactions}

## Original task

{original}

## Recent main-chain excerpt

{recent or "No additional text or safe tool-result context was available."}

## PROGRESS.md

{progress}

## Repository state

{repository}
"""
    brief, final_count = scrub_secrets(brief)
    if final_count:
        redactions += final_count
        brief = brief.replace(
            f"Credential/binary redactions in this brief: {redactions - final_count}",
            f"Credential/binary redactions in this brief: {redactions}",
        )
    return brief.rstrip() + "\n", original


def run(
    session_id: str | None,
    last: bool,
    target: str,
    workdir: str | Path | None,
    delegate_main: Callable[[list[str]], int] = delegate.main,
) -> int:
    """Build and dispatch one detached handoff; intended as the CLI handler seam."""
    try:
        if target not in TARGETS:
            raise HandoffError(f"--to must be one of {', '.join(sorted(TARGETS))}")
        if target in delegate.RETIRED_MODEL_ALIASES:
            replacement = delegate.RETIRED_MODEL_ALIASES[target]
            print(
                "subfleet handoff: " + delegate.RETIRED_MODEL_NOTE.format(
                    alias=target, replacement=replacement,
                ),
                file=sys.stderr,
            )
            target = replacement
        canonical, transcript = _resolve_source(session_id, last)
        cwd, source_cwd = _workdir(transcript, workdir)
        brief, original = _build_brief(canonical, transcript, cwd, source_cwd)
        task_class, _signals = delegate.classify(original)
    except HandoffError as exc:
        print(f"subfleet handoff: {exc}", file=sys.stderr)
        return 2

    prompt_path: Path | None = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix="subfleet-handoff-", suffix=".md")
        prompt_path = Path(raw_path)
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(brief)
        argv = [
            "-d",
            "-m",
            target,
            "-t",
            task_class,
            "-C",
            str(cwd),
            "-n",
            f"handoff-{canonical[:8]}",
            "-p",
            str(prompt_path),
        ]
        return int(delegate_main(argv))
    except OSError as exc:
        print(f"subfleet handoff: {exc}", file=sys.stderr)
        return 1
    finally:
        if prompt_path is not None:
            try:
                prompt_path.unlink()
            except OSError:
                pass

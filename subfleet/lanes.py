"""Lane sessions: headless ``claude -p`` runs launched by subfleet.

A lane is a one-shot worker whose captured deliverable is its LAST assistant
message. Anything that addresses it as if it were an interactive session —
a resume nudge, a broadcast, a `subfleet notify` — appends a turn and
overwrites that deliverable (2026-09-04: the Sol→Astra routing broadcast
turned two finished lanes' ``.out`` files into "Acknowledged — no action
needed"). So lane sessions are recognised here, hidden from the session
registry by default, and refused as notify targets unless forced.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import capacity, paths
from .util import load_json

HEADLESS_ENTRYPOINT = "sdk-cli"
HEADLESS_PROMPT_SOURCE = "sdk"


def headless_transcript(transcript: str | Path | None, *, max_lines: int = 5000) -> bool:
    """True for a ``claude -p`` (SDK) session — a lane run, probe, or one-shot.

    Measured 2026-09-04 on every kind of session on this machine: a lane's
    transcript holds exactly ONE text prompt (its brief) and it arrived via
    the SDK (``promptSource: sdk``); tool results are user entries too but
    carry no promptSource. Interactive sessions differ in one of two ways: a
    human-typed prompt (``promptSource`` ``typed`` in the tmux CLI, absent in
    the desktop app), or MANY sdk-sourced text prompts, because inbox notices
    (``subfleet notify``) arrive as ``sdk`` — the ceremony session had 93. A
    lane that was itself notified may show a second sdk prompt, so up to two
    are still a lane. The entrypoint does not discriminate (lanes launched
    through the desktop-bundled binary read ``claude-desktop``).
    """
    if not transcript:
        return False
    text_prompts = 0
    first = None
    try:
        with Path(transcript).open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") != "user" or entry.get("isMeta"):
                    continue
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, list) and content and all(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                ):
                    continue  # tool results, not prompts
                source = entry.get("promptSource")
                if source != HEADLESS_PROMPT_SOURCE:
                    return False  # a typed (or desktop, promptSource-less) human prompt
                text_prompts += 1
                if first is None:
                    first = source
                if text_prompts > 2:
                    return False  # an inbox-driven interactive session
    except OSError:
        return False
    return first == HEADLESS_PROMPT_SOURCE

def lane_session_ids() -> set[str]:
    """Session ids of every Claude lane run subfleet launched (runs/*/meta.json)
    plus every lane run the capacity ledger recorded — never resumable."""
    ids: set[str] = set()
    try:
        for meta in paths.runs_dir().glob("*/meta.json"):
            data = load_json(meta)
            sid = data.get("session_id") if isinstance(data, dict) else None
            if isinstance(sid, str) and sid:
                ids.add(sid)
    except OSError:
        pass
    try:
        for record in capacity.read_ledger():
            sid = record.get("session_id") if isinstance(record, dict) else None
            if isinstance(sid, str) and sid:
                ids.add(sid)
    except Exception:  # a broken ledger must not break the sweep
        pass
    return ids



def is_lane_session(session_id: str, transcript: str | Path | None = None,
                    ids: set[str] | None = None) -> bool:
    """True when subfleet launched this session as a lane, or its transcript
    shows a headless (SDK-prompted) first turn."""
    known = ids if ids is not None else lane_session_ids()
    if session_id in known:
        return True
    return headless_transcript(transcript) if transcript else False

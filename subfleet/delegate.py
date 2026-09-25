"""Content- and capacity-aware dispatch to the hardened agent runners."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from . import capacity, notify, reserve, run_ledger
from . import claude as claude_side
from . import codex as codex_side
from .util import parse_iso

FABLE_PATTERNS = (
    r"\bas\s+max\b", r"\bvoice\b", r"\bemails?\b", r"\bblogs?\b",
    r"\bessays?\b", r"\bprose\b", r"\badjudicat(?:e|es|ed|ing|ion|ions)\b",
    r"\bverdicts?\b", r"\bfinal\s+review\b", r"\bmerge[-\s]+gate\b",
    r"\blaunch(?:es|ed|ing)?\b", r"\bsend(?:s|ing)?\b",
    r"\bdesign(?:s|ed|ing)?\b", r"\bstrateg(?:y|ies|ic|ically)\b", r"\bwdyt\b",
)
REVIEW_PATTERNS = (
    r"\breview(?:s|ed|ing|er|ers)?\b",
    r"\bassess(?:es|ed|ing|ment|ments|or|ors)?\b",
    r"\bcritiqu(?:e|es|ed|ing)\b",
    r"\baudit(?:s|ed|ing|or|ors)?\b",
    r"\bevaluat(?:e|es|ed|ing|ion|ions|or|ors)\b",
    r"\breferee(?:s|d|ing)?\b",
)
SWEEP_PATTERNS = (r"for each", r"per-file", r"per-item", r"per-row", r"batch of", r"enumerate", r"across all")
MECHANICAL_PATTERNS = (r"verify", r"count", r"list", r"extract", r"check")
BUILD_PATTERNS = (r"implement", r"fix", r"refactor", r"port", r"migrate", r"wire", r"test")

MODEL_FAMILY = {
    "fable": "claude", "opus": "claude", "sonnet": "claude", "haiku": "claude",
    "sol": "codex", "terra": "codex", "astra": "codex", "luna": "codex",
}
MODEL_NAMES = {
    "fable": "claude-fable-5-1", "opus": "claude-opus-5",
    "sonnet": "sonnet",
    "haiku": "claude-haiku-4-5-20251001",
    "sol": "gpt-5.6-sol", "terra": "gpt-5.6-terra",
    # GPT-6 Astra: served to ChatGPT-account Codex lanes since codex CLI 0.153
    # (catalog minimal_client_version 0.153.0; hidden from the model picker but
    # dispatchable; shares the "codex" weekly window). Verified 2026-09-04.
    "astra": "gpt-6-astra",
    # GPT-5.6 Luna: the catalog's "fast and affordable agentic coding model"
    # (models_cache.json: visibility "list", default reasoning "medium", 272k
    # context), served to ChatGPT-account Codex lanes on the same "codex"
    # weekly window. Max (2026-09-11): use it for the simple subagent work, so
    # the trivial and easy tiers route here instead of to Haiku or Sonnet,
    # which would draw a Claude account's shared weekly bucket (the 9/6 rule).
    "luna": "gpt-5.6-luna",
}
CODEX_MODEL_CHOICES = ("sol", "terra", "astra", "luna")
# Frontier Codex models run at the top reasoning level (Codex's own default
# for gpt-6-astra is "low").
CODEX_ULTRA_EFFORT_MODELS = frozenset({"sol", "astra"})
# Retired from dispatch (Max, 2026-09-04: "stop using sol subagents for
# anything. for basic stuff we should use opus, hard stuff astra"). The alias
# still parses so older scripts, gate states, and handoffs keep working; every
# entry point remaps it and says so on stderr.
RETIRED_MODEL_ALIASES = {"sol": "astra"}
# Legacy coarse classes carry no tier; these two are routed as the standard
# tier (Opus, moving upward to Astra only when Opus is conclusively exhausted).
LEGACY_STANDARD_CLASSES = frozenset({"build", "review"})
# The frontier Codex model a `-H <codex home>` pin implies when no -m is given.
CODEX_HOME_DEFAULT_MODEL = "astra"
RETIRED_MODEL_NOTE = (
    "{alias} is retired from dispatch (Max 2026-09-04: Opus for standard work, "
    "Astra for hard work); dispatching {replacement} instead"
)
CLAUDE_MODEL_DISPLAY = {
    "fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku",
}
SEMANTIC_TASKS = (
    "lookup", "research", "sweep", "review", "build",
    "authored-prose", "strategy", "adjudication",
)
TIERS = ("trivial", "easy", "standard", "hard")
FABLE_TASKS = {"authored-prose", "strategy", "adjudication"}
SEMANTIC_MODEL_GRID = {
    **{
        task: {
            "trivial": "luna", "easy": "luna", "standard": "opus", "hard": "astra",
        }
        for task in ("lookup", "research", "review", "build")
    },
    "sweep": {
        "trivial": "terra", "easy": "terra", "standard": "terra", "hard": "astra",
    },
    **{
        task: {tier: "fable" for tier in TIERS}
        for task in FABLE_TASKS
    },
}
UPWARD_MODEL_CHAINS = {
    "trivial": ("luna", "opus", "astra"),
    "easy": ("luna", "opus", "astra"),
    "standard": ("opus", "astra"),
    "hard": ("astra",),
}
PREAMBLE_WRITE = ("Standing orders: commit after every coherent step; create and maintain a committed "
                  "PROGRESS.md (state/done/next) from the start; write your final report to the output file.")
PREAMBLE_AUDIT = "Frame this as a defensive correctness and completeness audit."
REENROLL_RITUAL = ("Re-enroll lane {email}: run `claude setup-token` while signed into {email}, then store "
                   "it in the keychain item `claude-quota-{email}` (or run `subfleet enroll {email}`).")


def _now() -> datetime:
    return datetime.now().astimezone()


def _state_dir() -> Path:
    return Path(os.environ.get("DELEGATE_STATE_DIR", "~/.local/state/delegate")).expanduser()


def _accounts_file() -> Path:
    return Path(
        os.environ.get("DELEGATE_ACCOUNTS_FILE")
        or Path(__file__).resolve().parent.parent / "claude-accounts.json"
    ).expanduser()


def _matches(prompt: str, patterns: Sequence[str]) -> list[str]:
    return [p for p in patterns if re.search(p, prompt, re.IGNORECASE)]


def classify(prompt: str, forced: str | None = None) -> tuple[str, dict[str, list[str]]]:
    signals = {
        "fable": _matches(prompt, FABLE_PATTERNS),
        "review": _matches(prompt, REVIEW_PATTERNS),
        "sweep": _matches(prompt, SWEEP_PATTERNS),
        "mechanical": _matches(prompt, MECHANICAL_PATTERNS),
        "build": _matches(prompt, BUILD_PATTERNS),
    }
    if forced:
        return forced, signals
    if signals["fable"]:
        return "fable", signals
    if signals["review"]:
        return "review", signals
    if signals["sweep"] and signals["mechanical"]:
        return "sweep", signals
    return "build", signals


def semantic_model_candidates(task: str, tier: str) -> tuple[str, ...]:
    """Ordered minimum-capability route for an explicitly classified task."""
    preferred = SEMANTIC_MODEL_GRID[task][tier]
    if task in FABLE_TASKS:
        return ("fable",)
    if task == "sweep":
        return (preferred,)
    return UPWARD_MODEL_CHAINS[tier]


def resolve_retired_alias(model: str | None, *, stream=None) -> str | None:
    """Map a retired alias to its replacement, announcing the swap on `stream`
    (stderr by default). Non-retired aliases pass through unchanged."""
    if model in RETIRED_MODEL_ALIASES:
        replacement = RETIRED_MODEL_ALIASES[model]
        print(
            "delegate: " + RETIRED_MODEL_NOTE.format(alias=model, replacement=replacement),
            file=stream or sys.stderr,
        )
        return replacement
    return model


def choose_model(task_class: str, explicit: str | None = None,
                 tier: str | None = None) -> str:
    if explicit:
        return RETIRED_MODEL_ALIASES.get(explicit, explicit)
    if tier is not None:
        return SEMANTIC_MODEL_GRID[task_class][tier]
    # Legacy coarse classes carry no tier: standard work goes to Opus; ask for
    # `--tier hard` or `-m astra` when the work is hard.
    return {"fable": "fable", "review": "opus", "sweep": "terra", "build": "opus"}[task_class]


def _load_cooldowns() -> dict[str, dict[str, str]]:
    return capacity.read_lane_cooldowns()


def _save_cooldowns(data: dict) -> None:
    path = _state_dir() / "cooldowns.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = capacity._normalize_cooldown_data(data)
    path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")


def _rotation() -> dict[str, str]:
    try:
        return json.loads((_state_dir() / "rotation.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _set_last_used(email: str) -> None:
    path = _state_dir() / "rotation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_used": email}, indent=2) + "\n")


def _enrolled() -> list[str]:
    try:
        enrolled = json.loads(_accounts_file().read_text()).get("enrolled", {})
        return [
            email for email, secret in enrolled.items()
            if isinstance(email, str) and "@" in email
            and isinstance(secret, str) and bool(secret.strip())
        ] if isinstance(enrolled, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def _active_desktop_email() -> str | None:
    binary = os.environ.get("DELEGATE_SUBFLEET", _repo_bin("subfleet"))
    try:
        cp = subprocess.run([binary, "status", "--cached", "--json"], capture_output=True, text=True)
        if cp.returncode:
            return None
        data = json.loads(cp.stdout)
        # Accommodate both snapshot layouts and future additive changes.
        for row in data.get("claude", {}).get("accounts", data.get("claude_accounts", [])):
            if row.get("active"):
                return row.get("email")
        return data.get("claude", {}).get("active_email")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def pick_fable_lane(exclude: set[str] | None = None) -> str | None:
    """Pick and persist one optimistic Claude lane; the sole replaceable seam."""
    exclude = exclude or set()
    now = _now()
    live = []
    for email in _enrolled():
        until = capacity.lane_cooldown(
            email, model=MODEL_NAMES["fable"], now=now
        )
        if email not in exclude and until is None:
            live.append(email)
    if not live:
        return None
    last = _rotation().get("last_used")
    if last in live:
        pos = (live.index(last) + 1) % len(live)
        live = live[pos:] + live[:pos]
    active = _active_desktop_email()
    if len(live) > 1 and live[0] == active:
        live.append(live.pop(0))
    picked = live[0]
    _set_last_used(picked)
    return picked


def record_cooldown(email: str, until: datetime, *, model: str | None = None) -> None:
    capacity.store_lane_cooldown(email, until, model=model)


def _explicit_limited_until(text: str) -> datetime | None:
    match = re.search(r"resets\s+(\d{1,2}):(\d{2})\s*(am|pm)", text, re.IGNORECASE)
    if not match:
        return None
    now = _now()
    hour, minute, meridiem = int(match[1]), int(match[2]), match[3].lower()
    hour = hour % 12 + (12 if meridiem == "pm" else 0)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def _limited_until(text: str) -> datetime:
    return _explicit_limited_until(text) or (_now() + timedelta(minutes=60))


def _future_cooldown(email: str, *, model: str | None = None) -> datetime | None:
    return capacity.lane_cooldown(email, model=model, now=_now())


def _append_decision(record: dict[str, Any]) -> None:
    path = _state_dir() / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _output_path_guard(out: str | None, *, reuse: bool = False) -> int | None:
    """Refuse an ``-o`` path that a live run is still writing; name the last writer.

    Two runs launched onto one path write one deliverable between them and
    the file can no longer be attributed to a run (2026-09-04, doc_076: a
    killed misroute and its pinned replacement). Returns an exit code to stop
    with, or None to proceed.
    """
    if not out:
        return None
    hits = run_ledger.output_collisions(out)
    live = [hit for hit in hits if hit.get("live")]
    if live and not reuse:
        hit = live[0]
        print(
            f"delegate: refusing -o {out}: run {hit['id']} (lane {hit.get('lane') or '-'}, "
            f"pid {hit.get('pid')}) is still writing it — suffix the path with the run id, or "
            "`subfleet kill` that run first (--reuse-out overrides)",
            file=sys.stderr,
        )
        return 3
    if hits:
        hit = hits[0]
        print(
            f"delegate: note: -o {out} was last named by run {hit['id']} "
            f"({hit.get('finished_at') or 'unfinished'}); resolve the landing by run id, "
            "not by whatever file sits at the path",
            file=sys.stderr,
        )
    return None


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="subfleet run")
    task = p.add_mutually_exclusive_group()
    task.add_argument("-t", choices=("fable", "review", "build", "sweep"),
                      help="legacy coarse task-class override")
    task.add_argument("--task", choices=SEMANTIC_TASKS,
                      help="semantic task type; requires --tier")
    p.add_argument("--tier", choices=TIERS,
                   help="minimum capability for --task")
    p.add_argument("-m", choices=("fable", "opus", "sonnet", "sol", "terra", "astra", "haiku", "luna"),
                   help="exact model override (disables capacity fallback); "
                        "astra = GPT-6 Astra on a ChatGPT-subscription Codex lane; "
                        "sol is retired and dispatches astra")
    resources = p.add_mutually_exclusive_group()
    resources.add_argument("-a", metavar="EMAIL")
    resources.add_argument("-H", metavar="CODEX_HOME")
    p.add_argument("-C", default=os.getcwd())
    p.add_argument("-o")
    p.add_argument("-s", choices=("read-only", "workspace-write"))
    p.add_argument("-d", "--detach", dest="d", action="store_true",
                   help="launch detached and return at once (the default inside a Claude session)")
    p.add_argument("--attach", "--wait", dest="attach", action="store_true",
                   help="block until the run finishes (inside a Claude session the provider is still "
                        "detached, so a session restart cannot kill it; re-join with `subfleet wait <id>`)")
    p.add_argument("-n", "--name", dest="name", help="short label for the ledger id / run table")
    p.add_argument("--json", action="store_true", help="machine-readable dispatch line")
    p.add_argument("-b")
    p.add_argument("--overflow", action="store_true")
    p.add_argument("--no-preamble", action="store_true")
    p.add_argument("-x", "--exclude", action="append", default=[], metavar="EMAIL",
                   help="never pick this Claude lane account (repeatable; e.g. the accounts that ran a pair's lanes)")
    p.add_argument("--reuse-out", action="store_true",
                   help="dispatch onto an -o path that a LIVE run is still writing (default: refuse; "
                        "a finished run's path is only noted)")
    p.add_argument("--independent-review", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--review-root", help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--why", action="store_true")
    p.add_argument("--status", action="store_true")
    source = p.add_mutually_exclusive_group()
    source.add_argument("-p", metavar="PROMPTFILE")
    source.add_argument("prompt", nargs="?")
    return p


def _status() -> int:
    last, now = _rotation().get("last_used"), _now()
    print(f"Claude lanes (last_used={last or '-'}):")
    for email in _enrolled():
        account = capacity.lane_cooldown(email, now=now)
        model_states = []
        for alias in ("fable", "opus", "sonnet", "haiku"):
            until = capacity.lane_cooldown(
                email, model=MODEL_NAMES[alias], now=now
            )
            state = f"cooled-until-{until.isoformat()}" if until else "ok"
            model_states.append(f"{alias}={state}")
        overall = f"account-cooldown-until-{account.isoformat()}" if account else "available"
        print(f"  {email}: {overall} · {' '.join(model_states)}")
    try:
        cp = subprocess.run([os.environ.get("DELEGATE_SUBFLEET", _repo_bin("subfleet")), "pick", "codex", "--json", "--all"], text=True, capture_output=True)
        sys.stdout.write(cp.stdout)
        sys.stderr.write(cp.stderr)
    except OSError as exc:
        print(f"delegate: subfleet pick codex unavailable: {exc}", file=sys.stderr)
    return 0


def _prompt_text(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if args.p:
        try:
            return Path(args.p).read_text()
        except OSError as exc:
            parser.error(str(exc))
    if args.prompt is None:
        parser.error("one of -p PROMPTFILE or PROMPT_TEXT is required")
    return args.prompt


def _codex_home() -> tuple[str | None, str]:
    try:
        cp = subprocess.run([os.environ.get("DELEGATE_SUBFLEET", _repo_bin("subfleet")), "pick", "codex"], capture_output=True, text=True)
    except OSError as exc:
        return None, str(exc) + "\n"
    return (cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None), cp.stderr


def _capacity_report() -> dict[str, Any]:
    """Single mockable seam for the cached live probes + dynamic lane ledger."""
    try:
        return capacity.report(accounts_file=_accounts_file())
    except Exception as exc:
        return {
            "generated_at": _now().isoformat(),
            "cache": {"hit": False, "error": str(exc)},
            "accounts": [],
            "families": {
                "codex": {"available": False, "all_limited": False, "state": "unknown",
                          "headroom_score": None, "earliest_reset": None},
                "claude": {"available": False, "all_limited": False, "state": "unknown",
                           "headroom_score": None, "earliest_reset": None},
            },
        }


#: A blind lane (no usage reading at all) may carry at most this many
#: concurrent runs. 2026-09-03: five Opus lanes were spread across accounts
#: whose usage subfleet could not see (keepalive probes failing → score None,
#: still "dispatchable"), and all five died mid-run when those accounts capped.
BLIND_LANE_MAX_IN_FLIGHT = 0
#: Just-in-time probe results are reused within a process for this long.
JIT_PROBE_TTL_S = 300.0
_jit_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _probe_lane_headroom(email: str) -> dict[str, Any]:
    """Live OAuth usage probe with the lane's own enrolled token.

    Returns ``{"score": float|None, "reset_at": str|None, "status": str}``.
    ``score`` is the worst-window headroom in percent (``capacity._score``);
    ``None`` means the probe produced no numbers (no token, token invalid,
    endpoint throttling, network). The sole replaceable seam for tests."""
    cached = _jit_probe_cache.get(email)
    if cached and time.monotonic() - cached[0] < JIT_PROBE_TTL_S:
        return cached[1]
    enrolled = (claude_side.roster_config().get("enrolled") or {})
    secret = enrolled.get(email) if isinstance(enrolled, dict) else None
    token = claude_side.agent_secret_get(secret) if isinstance(secret, str) and secret else None
    probe = claude_side.probe_oauth_usage(token, timeout=10.0)
    if probe.get("status") != "ok":
        # The setup token cannot read the usage endpoint (standing 429); the
        # account's full-scope login dir (subfleet-v2) can. Same account, same
        # windows -- a real measurement instead of a blind lane.
        fallback = reserve.headroom_probe(email)
        if fallback is not None:
            probe = fallback
    five_hour = probe.get("five_hour") or {}
    weekly = probe.get("seven_day") or {}
    result = {
        "status": probe.get("status"),
        "score": capacity._score(five_hour, weekly) if probe.get("status") == "ok" else None,
        "reset_at": five_hour.get("reset_at") or weekly.get("reset_at"),
    }
    _jit_probe_cache[email] = (time.monotonic(), result)
    return result


def _blind_lane_filter(rows: list[dict[str, Any]], model_family: str,
                       score_of) -> list[dict[str, Any]]:
    """Probe lanes whose headroom is unknown before letting them be picked.

    A measured lane below the headroom floor is dropped and cooled until its
    window resets; a lane that stays blind is allowed only while it carries
    no other run. Measured lanes are annotated so the caller's sort prefers
    them over the remaining blind ones."""
    kept = []
    for row in rows:
        if score_of(row) is not None:
            kept.append(row)
            continue
        email = str(row.get("email") or row.get("id") or "")
        probe = _probe_lane_headroom(email) if email else {"score": None, "status": "no-email"}
        score = probe.get("score")
        if score is None:
            if int(row.get("in_flight") or 0) > BLIND_LANE_MAX_IN_FLIGHT:
                continue
            row = dict(row)
            row["jit_probe"] = probe.get("status")
            kept.append(row)
            continue
        if score < capacity.DEFAULT_MIN_HEADROOM:
            until = capacity._parse_time(probe.get("reset_at")) or (_now() + timedelta(minutes=60))
            try:
                capacity.store_lane_cooldown(email, until, model=capacity.normalize_claude_model(model_family))
            except OSError:
                pass
            continue
        row = dict(row)
        row["headroom_score"] = score
        row["dispatch_score"] = score
        row["five_hour"] = dict(row.get("five_hour") or {}, used_percent=100.0 - score)
        row["weekly"] = dict(row.get("weekly") or {}, used_percent=100.0 - score)
        row["confidence"] = "jit-probe"
        row["jit_probe"] = "ok"
        kept.append(row)
    return kept


def _capacity_candidates(data: dict[str, Any], family: str,
                         exclude: set[str] | None = None, *,
                         model_family: str | None = None,
                         probe_blind: bool = True) -> list[dict[str, Any]]:
    exclude = exclude or set()
    rows = [
        row for row in data.get("accounts", [])
        if row.get("family") == family and row.get("dispatchable")
        and str(row.get("email") if family == "claude" else row.get("id")) not in exclude
    ]
    if family == "claude" and model_family:
        model = capacity.normalize_claude_model(model_family)
        rows = [
            row for row in rows
            if capacity.dispatchable_for(row, model_family)
            and capacity.lane_cooldown(
                str(row.get("email") or row.get("id") or ""),
                model=model,
                now=_now(),
            ) is None
        ]
        if probe_blind:
            rows = _blind_lane_filter(
                rows, model_family,
                lambda row: capacity.model_headroom_score(row, model_family),
            )

    def raw_tokens(row: dict[str, Any]) -> tuple[float, float]:
        weekly = (row.get("weekly") or {}).get("tokens")
        five_hour = (row.get("five_hour") or {}).get("tokens")
        return (
            float(weekly) if isinstance(weekly, (int, float)) else float("inf"),
            float(five_hour) if isinstance(five_hour, (int, float)) else float("inf"),
        )

    if family == "codex":
        rows.sort(
            key=lambda row: (
                row.get("dispatch_score") is None,
                -float(row.get("dispatch_score") or 0.0),
                str(row.get("id") or ""),
            )
        )
        return rows

    # A measured/calibrated score wins over an unknowable uncalibrated one;
    # within either group, maximize worst-window headroom then spare the lane
    # with the lowest rolling token totals.
    def claude_score(row: dict[str, Any]) -> float | None:
        if model_family:
            score = capacity.model_headroom_score(row, model_family)
            if score is not None and row.get("active"):
                score -= capacity.INTERACTIVE_HANDICAP
            return score
        return row.get("dispatch_score", row.get("headroom_score"))

    # Max 8/26: for a non-fable request, spend fable-exhausted lanes FIRST —
    # their remaining capacity is stranded (fable can't use it), while opus
    # burn on a fable-capable lane eats the shared windows fable still needs.
    fable = capacity.normalize_claude_model("fable")
    target = capacity.normalize_claude_model(model_family) if model_family else None

    def fable_stranded(row: dict[str, Any]) -> bool:
        if target is None or target == fable:
            return False
        email = str(row.get("email") or row.get("id") or "")
        return (
            not capacity.dispatchable_for(row, fable)
            or capacity.lane_cooldown(email, model=fable, now=_now()) is not None
        )

    # Max (standing order, re-stated 2026-09-04): the desktop login is the
    # account Max's own sessions run on — a lane there competes with him.
    # It is the LAST resort: every other dispatchable lane, measured or
    # blind, ranks ahead of it. (The handicap alone was not enough: with
    # every lane token blind, the login was the only *measured* lane and
    # therefore "best" — exactly the lane subfleet should have spared.)
    rows.sort(
        key=lambda row: (
            not fable_stranded(row),
            bool(row.get("active")),
            claude_score(row) is None,
            -float(claude_score(row) or 0.0),
            int(row.get("in_flight") or 0),
            *raw_tokens(row),
            str(row.get("id") or row.get("email") or ""),
        )
    )
    return rows


def _reserve_filter(rows: list[dict[str, Any]], model: str,
                    pol: dict[str, Any] | None = None,
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fable reserve seam: lanes a non-Fable Claude model may use, and why not.

    Opus/Sonnet/Haiku draw the shared weekly window that Fable also needs, so
    a lane whose shared headroom would not cover its remaining Fable share is
    withheld (Max, 2026-09-06; see ``reserve``). Fable and a disabled policy
    pass every row through. Each kept row comes back annotated with the
    reading that kept it (``row["reserve"]``); each drop names its lane, state
    and reading.
    """
    return reserve.filter_lanes(rows, model=MODEL_NAMES[model], pol=pol)


RESERVE_LANE_KEYS = ("state", "slack", "shared", "fable", "status", "checked_at")


def _reserve_record(kept: list[dict[str, Any]], drops: list[dict[str, Any]],
                    pol: dict[str, Any]) -> dict[str, Any]:
    """The ``reserve`` entry of a decision record for one non-Fable Claude attempt.

    Every attempt records what the reserve saw, not only the attempts it
    blocked: the policy in force, each lane the filter kept (with the reading
    that kept it), each lane it dropped, and -- set by the caller once known --
    what happened next (``dispatched``, ``upgraded to <model>``, ``no lane``,
    ``attempt cap``). Until 2026-09-18 an ordinary Opus dispatch logged
    ``reserve: null`` even when the filter had withheld lanes, so the log could
    not show why one lane was chosen over another. Lane entries carry
    percentages, states and timestamps only (``RESERVE_LANE_KEYS``); no token
    value ever reaches a decision record.
    """
    return {
        "policy": {key: pol[key] for key in ("enabled", "cap_ratio", "min_slack")},
        "kept": [
            {
                "lane": str(row.get("email") or row.get("id") or ""),
                **{key: (row.get("reserve") or {}).get(key) for key in RESERVE_LANE_KEYS},
            }
            for row in kept
        ],
        "drops": [
            {"lane": drop.get("lane"), **{key: drop.get(key) for key in RESERVE_LANE_KEYS}}
            for drop in drops
        ],
        "action": None,
    }


def _scoped_limit_reasoning(data: dict[str, Any], model_family: str) -> dict[str, Any]:
    """Explain which otherwise-healthy Claude lanes a model scope excludes."""
    globally_available = _capacity_candidates(data, "claude")
    model = capacity.normalize_claude_model(model_family)
    blocked = []
    for row in globally_available:
        email = str(row.get("email") or row.get("id") or "?")
        # Mirror the candidate filter exactly: the persisted per-model cooldown
        # shuts a lane even when the capacity row carries no cooldown of its own.
        file_cooldown = capacity.lane_cooldown(email, model=model, now=_now())
        if capacity.dispatchable_for(row, model_family) and file_cooldown is None:
            continue
        limit = capacity.scoped_limit_for(row, model_family)
        # A lane can be shut for this model by a cooldown alone (a 5h window
        # tripped earlier today) with no scoped limit on record; say which.
        cooldown = capacity.model_cooldown_for(row, model_family) or (
            file_cooldown.isoformat() if file_cooldown else None
        )
        blocked.append(
            {
                "lane": email,
                "limit": limit,
                "cooldown_until": cooldown,
            }
        )
    eligible = _capacity_candidates(
        data, "claude", model_family=model_family
    )
    return {
        "model_family": model_family,
        "eligible_lanes": [
            str(row.get("email") or row.get("id") or "?") for row in eligible
        ],
        "blocked_lanes": blocked,
    }


def _earliest_scoped_reset(reasoning: dict[str, Any]) -> str | None:
    resets = []
    for blocked in reasoning.get("blocked_lanes") or []:
        for value in ((blocked.get("limit") or {}).get("resets_at"), blocked.get("cooldown_until")):
            parsed = parse_iso(value)
            if parsed is not None:
                resets.append((parsed, value))
    return min(resets, key=lambda item: item[0])[1] if resets else None


def _blocked_reason(reasoning: dict[str, Any], model_family: str) -> str:
    """Name what actually shuts the lanes: scoped limits, cooldowns, or both."""
    blocked = reasoning.get("blocked_lanes") or []
    limited = [b for b in blocked if b.get("limit")]
    cooled = [b for b in blocked if not b.get("limit") and b.get("cooldown_until")]
    if limited and not cooled:
        return f"all dispatchable Claude lanes have exhausted {model_family} scoped limits"
    if cooled and not limited:
        return (
            f"all dispatchable Claude lanes are in a {model_family} cooldown "
            f"(5h windows tripped earlier; no scoped limit on record)"
        )
    return (
        f"all dispatchable Claude lanes are shut for {model_family}: "
        f"{len(limited)} at a scoped limit, {len(cooled)} in cooldown"
    )


def _family_available(data: dict[str, Any], family: str) -> bool:
    return bool((data.get("families", {}).get(family) or {}).get("available"))


def _model_capacity_state(data: dict[str, Any], model: str) -> bool | None:
    """Return True/False for known capacity, or None when telemetry is inconclusive."""
    family = MODEL_FAMILY[model]
    if family == "codex":
        if _capacity_candidates(data, "codex"):
            return True
    else:
        if _capacity_candidates(
            data, "claude", model_family=CLAUDE_MODEL_DISPLAY[model]
        ):
            return True
        # Lanes eligible for this model that the blind-lane policy withheld
        # (no usage reading and another run in flight) are not evidence of a
        # model-scoped limit; the telemetry is inconclusive. 2026-09-05: five
        # blind Opus lanes were withheld and read as "opus exhausted".
        if _capacity_candidates(
            data, "claude", model_family=CLAUDE_MODEL_DISPLAY[model],
            probe_blind=False,
        ):
            return None
        # Healthy lanes that are all excluded for this model by a scoped
        # limit or cooldown are conclusive evidence of a model-scoped limit,
        # even if the account itself is fine.
        if _capacity_candidates(data, "claude"):
            return False

    summary = data.get("families", {}).get(family) or {}
    if summary.get("all_limited"):
        return False
    family_rows = [
        row for row in data.get("accounts", []) if row.get("family") == family
    ]
    if not family_rows and summary.get("state") == "empty":
        return False
    return None


def select_semantic_model(
    data: dict[str, Any], candidates: Sequence[str]
) -> tuple[str, dict[str, bool | None]]:
    """Choose the first non-exhausted model without ever moving down-tier."""
    states = {model: _model_capacity_state(data, model) for model in candidates}
    for model in candidates:
        # Unknown telemetry is not evidence that a requested model is spent.
        if states[model] is not False:
            return model, states
    # Keep the highest-capability endpoint when the entire chain is known
    # exhausted. It may still be recoverable via an explicit reset credit.
    return candidates[-1], states


def _capacity_view(data: dict[str, Any]) -> dict[str, Any]:
    """The sanitized probe inputs and scores persisted with every decision."""
    return {
        key: data.get(key)
        for key in ("generated_at", "cache", "accounts", "families")
    }


def _repo_bin(name: str) -> str:
    return str(Path(__file__).resolve().parent.parent / "bin" / name)


def _repo_claude_lane() -> str:
    return _repo_bin("subfleet-claude")


def launch_mode(args: argparse.Namespace, env: dict[str, str] | None = None) -> tuple[str, str]:
    """Decide how the provider process is launched: ('detached'|'sync', why).

    Inside a Claude Code session the tool shell's process tree dies on an
    account switch or the desktop app's idle SIGTERM, so the provider is
    launched in its own process session by default and `subfleet run` returns
    immediately; `--attach` keeps the detached launch but waits inline.
    Outside a session (launchd lane scripts, a terminal) the synchronous
    runner path is unchanged; `-d` / SUBFLEET_RUN_DETACH=1 detach anywhere.
    """
    env = os.environ if env is None else env
    if args.dry_run:
        return "sync", "dry-run"
    if args.d:
        return "detached", "-d"
    override = (env.get("SUBFLEET_RUN_DETACH") or "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return "sync", "SUBFLEET_RUN_DETACH=0"
    if override in {"1", "true", "yes", "on"}:
        return "detached", "SUBFLEET_RUN_DETACH=1"
    if notify.in_claude_session(env):
        if args.attach:
            return "detached", "inside a Claude session; --attach waits inline"
        return "detached", "inside a Claude session — the provider must outlive it"
    return "sync", "outside a Claude session"


def _short_home(value: str | None) -> str:
    if not value:
        return "-"
    home = str(Path.home())
    return "~" + value[len(home):] if value.startswith(home) else value


def _default_slug(args: argparse.Namespace, prompt: str) -> str:
    if args.name:
        return args.name
    if args.o:
        return Path(args.o).name
    if args.p:
        return Path(args.p).name
    words = re.findall(r"[A-Za-z0-9]+", prompt)[:5]
    return "-".join(words).lower() or "run"


def _announce(*, run_id: str, pid: int | None, model: str, lane: str | None,
              output: str, lane_log: str, reason: str, wait_inline: bool,
              as_json: bool, notify_target: dict[str, Any] | None) -> None:
    target = None
    if notify_target:
        target = notify_target.get("name") or notify_target.get("session_id")
    if as_json:
        print(json.dumps({
            "run_id": run_id, "pid": pid, "model": model, "lane": lane,
            "out": output, "lane_log": lane_log, "detached": True,
            "wait_inline": wait_inline, "reason": reason,
            "notify_session": notify_target.get("session_id") if notify_target else None,
        }, sort_keys=True))
        return
    print(
        f"subfleet run: dispatched run={run_id} model={model} lane={_short_home(lane)}"
        f" pid={pid or '?'} (detached — {reason})"
    )
    print(f"  out: {output}")
    print(f"  log: {lane_log}")
    if wait_inline:
        print(f"  waiting inline; if this session restarts: subfleet wait {run_id}")
        return
    if target:
        print(f"  done → this session ({target}) gets a completion message; "
              f"to block instead: subfleet wait {run_id}   (ok under run_in_background)")
    else:
        print(f"  done → subfleet wait {run_id}   (blocks; ok under run_in_background)")
    print(f"  status: subfleet runs --mine · details: subfleet runs show {run_id} · cancel: subfleet kill {run_id}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.task and not args.tier:
        parser.error("--tier is required with --task")
    if args.tier and not args.task:
        parser.error("--tier requires --task")
    if args.independent_review:
        if not args.review_root:
            parser.error("--independent-review requires --review-root")
        if args.s != "read-only":
            parser.error("--independent-review requires -s read-only")
        if args.b:
            parser.error("--independent-review cannot be combined with -b")
        if not Path(args.review_root).expanduser().is_dir():
            parser.error("--review-root must be a directory")
    elif args.review_root:
        parser.error("--review-root requires --independent-review")
    if args.status:
        return _status()
    prompt = _prompt_text(args, parser)
    if args.task:
        _inferred_class, signals = classify(prompt)
        task_class = args.task
    else:
        task_class, signals = classify(prompt, args.t)
    semantic_candidates = (
        semantic_model_candidates(args.task, args.tier) if args.task else None
    )
    explicit_model = resolve_retired_alias(args.m)
    model = choose_model(task_class, explicit_model, args.tier)
    if args.H and explicit_model is None and MODEL_FAMILY[model] != "codex":
        # A pinned Codex home with no -m: run the frontier Codex model there.
        model = CODEX_HOME_DEFAULT_MODEL
    if (
        semantic_candidates is None and explicit_model is None and args.H is None
        and task_class in LEGACY_STANDARD_CLASSES
    ):
        semantic_candidates = UPWARD_MODEL_CHAINS["standard"]
    family = MODEL_FAMILY[model]
    requested_model, requested_family = model, family
    if args.a and family != "claude":
        parser.error("-a is only valid with a Claude model")
    if args.H and family != "codex":
        parser.error(f"-H is only valid with a Codex model ({', '.join(CODEX_MODEL_CHOICES)})")
    if args.H:
        api_refusal = codex_side.api_lane_refusal(args.H)
        if api_refusal:
            parser.error(api_refusal)
    sandbox = args.s or ("workspace-write" if task_class == "build" else "read-only")
    overrides = {
        key: value for key, value in {
            "class": args.t, "task": args.task, "tier": args.tier,
            "model": args.m, "lane": args.a, "home": args.H,
            "sandbox": args.s, "overflow": True if args.overflow else None,
            "independent_review": True if args.independent_review else None,
            "review_root": args.review_root,
        }.items() if value is not None
    }

    capacity_data = _capacity_report()
    routing_note = None
    routing_history: list[dict[str, str]] = []
    family_is_pinned = bool(args.m or args.a or args.H)
    semantic_capacity_states = None
    if semantic_candidates is not None and not family_is_pinned:
        selected_model, semantic_capacity_states = select_semantic_model(
            capacity_data, semantic_candidates
        )
        if selected_model != model:
            routing_history.append({
                "from": model,
                "to": selected_model,
                "reason": "capacity snapshot",
            })
            routing_note = (
                f"CAPABILITY FALLBACK: {model} has no dispatchable capacity; "
                f"routing {task_class}/{args.tier or 'standard'} upward to {selected_model}"
            )
            print(f"delegate: WARNING {routing_note}", file=sys.stderr)
            model, family = selected_model, MODEL_FAMILY[selected_model]
    reset_picked_home = None
    codex_rows = [
        row for row in capacity_data.get("accounts", [])
        if row.get("family") == "codex"
    ]
    has_applicable_reset = any(
        isinstance((row.get("reset_credits") or {}).get("applicable"), int)
        and not isinstance((row.get("reset_credits") or {}).get("applicable"), bool)
        and (row.get("reset_credits") or {}).get("applicable") > 0
        for row in codex_rows
    )
    if (
        family == "codex"
        and args.H is None
        and not _capacity_candidates(capacity_data, "codex")
        and has_applicable_reset
    ):
        reset_picked_home, picker_stderr = _codex_home()
        sys.stderr.write(picker_stderr)
    elastic_cross_family = args.task is None or (
        task_class == "sweep" and args.tier != "hard"
    )
    if (
        task_class in {"build", "review", "sweep"}
        and family == "codex"
        and not family_is_pinned
        and elastic_cross_family
        and reset_picked_home is None
        and bool((capacity_data.get("families", {}).get("codex") or {}).get("all_limited"))
        and bool(_capacity_candidates(
            capacity_data, "claude", model_family=CLAUDE_MODEL_DISPLAY["opus"]
        ))
    ):
        previous_model = model
        model, family = "opus", "claude"
        if args.task:
            routing_history.append({
                "from": previous_model,
                "to": model,
                "reason": "Codex fleet has no dispatchable headroom",
            })
        routing_note = (
            f"CROSS-FAMILY: Codex fleet has no dispatchable headroom; "
            f"routing elastic {task_class} work to Claude opus"
        )
        print(f"delegate: WARNING {routing_note}", file=sys.stderr)

    capacity_view = _capacity_view(capacity_data)
    scoped_reasoning = (
        _scoped_limit_reasoning(capacity_data, CLAUDE_MODEL_DISPLAY[model])
        if family == "claude" else None
    )
    family_scores = {
        name: (capacity_data.get("families", {}).get(name) or {}).get("headroom_score")
        for name in ("codex", "claude")
    }

    def next_semantic_route() -> tuple[str, dict[str, bool | None]] | None:
        if family_is_pinned or semantic_candidates is None:
            return None
        try:
            position = semantic_candidates.index(model)
        except ValueError:
            return None
        remaining = semantic_candidates[position + 1:]
        if not remaining:
            return None
        return select_semantic_model(capacity_data, remaining)

    def promote_semantic_model(
        selected_model: str,
        states: dict[str, bool | None],
        reason: str,
    ) -> None:
        nonlocal family, model, routing_note, scoped_reasoning
        previous_model = model
        model, family = selected_model, MODEL_FAMILY[selected_model]
        if semantic_capacity_states is not None:
            semantic_capacity_states[previous_model] = False
            semantic_capacity_states.update(states)
        routing_history.append({
            "from": previous_model,
            "to": selected_model,
            "reason": reason,
        })
        routing_note = (
            f"CAPABILITY FALLBACK: {previous_model} {reason}; "
            f"routing {task_class}/{args.tier} upward to {selected_model}"
        )
        scoped_reasoning = (
            _scoped_limit_reasoning(
                capacity_data, CLAUDE_MODEL_DISPLAY[selected_model]
            )
            if family == "claude" else None
        )
        print(f"delegate: WARNING {routing_note}", file=sys.stderr)

    reserve_note: dict[str, Any] | None = None

    def decision_record(lane_or_home: str | None, cmd: list[str],
                        result: int | None) -> dict[str, Any]:
        return {
            "ts": _now().isoformat(),
            "class": task_class,
            "task": args.task,
            "tier": args.tier,
            "routing_candidates": list(semantic_candidates) if semantic_candidates else None,
            "routing_capacity_states": semantic_capacity_states,
            "routing_history": routing_history,
            "requested_model": requested_model,
            "requested_family": requested_family,
            "model": model,
            "family": family,
            "lane/home": lane_or_home,
            "signals matched": signals,
            "overrides": overrides,
            "capacity": capacity_view,
            "family_scores": family_scores,
            "routing_note": routing_note,
            "reserve": reserve_note,
            "scoped_limit_reasoning": (
                {**scoped_reasoning, "selected_lane": lane_or_home}
                if scoped_reasoning is not None else None
            ),
            "result": result,
            "cmd": cmd,
        }

    def log_decision(lane_or_home: str | None, cmd: list[str], result: int) -> None:
        record = decision_record(lane_or_home, cmd, result)
        _append_decision(record)
        if args.why:
            print("delegate decision: " + json.dumps(record, sort_keys=True), file=sys.stderr)

    def runner_env(lane_or_home: str, cmd: list[str]) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("SUBFLEET_RUN_LANE_LOG", None)
        env["SUBFLEET_RUN_DECISION_JSON"] = json.dumps(
            decision_record(lane_or_home, cmd, None),
            sort_keys=True,
            separators=(",", ":"),
        )
        # Empty means the delegate manufactured a temporary output because the
        # caller did not supply -o; the ledger records that distinction as null.
        env["SUBFLEET_RUN_ORIGINAL_OUT"] = args.o or ""
        env.pop("SUBFLEET_RUN_ID", None)
        if caller is not None:
            env["SUBFLEET_RUN_CALLER_JSON"] = json.dumps(caller, sort_keys=True, separators=(",", ":"))
        else:
            env.pop("SUBFLEET_RUN_CALLER_JSON", None)
        return env

    # Fable is a quality floor, not an overflow preference. If no enrolled
    # Claude lane can dispatch, report the earliest recovery and stop rather
    # than silently converting final-adjudication/voice work to Sol.
    fable_floor_unavailable = (
        (task_class == "fable" or task_class in FABLE_TASKS)
        and model == "fable"
        and args.a is None
        and not _capacity_candidates(
            capacity_data, "claude", model_family=CLAUDE_MODEL_DISPLAY["fable"]
        )
    )
    if fable_floor_unavailable:
        summary = capacity_data.get("families", {}).get("claude") or {}
        blocked = (scoped_reasoning or {}).get("blocked_lanes") or []
        globally_available = _capacity_candidates(capacity_data, "claude")
        if globally_available and len(blocked) == len(globally_available):
            reason = _blocked_reason(scoped_reasoning or {}, "Fable")
            earliest = _earliest_scoped_reset(scoped_reasoning or {})
        elif summary.get("all_limited"):
            reason = "all Claude lanes are limited"
            earliest = summary.get("earliest_reset")
        elif summary.get("state") == "empty":
            reason = "no Claude lanes are enrolled"
            earliest = None
        else:
            reason = "Claude lane capacity is unknown"
            earliest = summary.get("earliest_reset")
        message = f"delegate: FABLE FLOOR unavailable: {reason}"
        if earliest:
            message += f"; earliest reset {earliest}"
        print(message + "; refusing cross-family downgrade", file=sys.stderr)
        log_decision(None, [], 3)
        return 3

    mode, mode_reason = launch_mode(args)
    wait_inline = bool(args.attach) and mode == "detached"
    caller = notify.caller_context(cwd=args.C)
    temp_paths: list[str] = []
    output = args.o
    if args.o and not args.dry_run:
        stop = _output_path_guard(args.o, reuse=bool(getattr(args, "reuse_out", False)))
        if stop is not None:
            log_decision(None, [], stop)
            return stop
    if not output and mode != "detached":
        fd, output = tempfile.mkstemp(prefix="delegate-output-", suffix=".md")
        os.close(fd); temp_paths.append(output)
    contents = prompt
    preamble = []
    if not args.no_preamble:
        if sandbox == "workspace-write":
            preamble.append(PREAMBLE_WRITE)
        if task_class == "review":
            preamble.append(PREAMBLE_AUDIT)
    if preamble:
        contents = "\n".join(preamble) + "\n\n" + prompt
    fd, merged = tempfile.mkstemp(prefix="delegate-prompt-", suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(contents)
    temp_paths.append(merged)

    def cleanup() -> None:
        for path in temp_paths:
            try:
                Path(path).unlink()
            except OSError:
                pass

    def launch_detached(lane_or_home: str, build_cmd) -> tuple[int, str | None, int | None]:
        """Pre-create the ledger entry, then start the runner in its own
        process session. Returns (result, run_id, pid)."""
        nonlocal output
        slug = _default_slug(args, prompt)
        run_caller = caller
        if wait_inline and caller is not None:
            # This process is the consumer; the completion push is only needed
            # if it dies before the run ends (notify.on_finish checks the pid).
            run_caller = {**caller, "waiter_pid": os.getpid()}
        run_id = run_ledger.start_run(
            family=family, model=MODEL_NAMES[model], lane=lane_or_home,
            workdir=args.C, prompt=merged, out=output,
            err=f"{output[:-3] if output and output.endswith('.md') else output}.err.log" if output else None,
            lane_log=f"{output[:-3] if output and output.endswith('.md') else output}.lane.log" if output else None,
            original_out=args.o or "", caller=run_caller, slug=slug, launcher="subfleet run",
        )
        paths_ = run_ledger.run_paths(run_id)
        output = paths_["out"]
        lane_log = paths_["lane_log"]
        cmd = build_cmd(output)
        launch_env = runner_env(lane_or_home, cmd)
        launch_env["SUBFLEET_RUN_LANE_LOG"] = lane_log
        launch_env["SUBFLEET_RUN_OWNED_PROMPT"] = merged
        launch_env["SUBFLEET_RUN_ID"] = run_id
        run_ledger.update_run(run_id, decision=decision_record(lane_or_home, cmd, None))
        try:
            with open(lane_log, "ab") as log_stream:
                proc = subprocess.Popen(
                    ["nohup", *cmd],
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=launch_env,
                )
        except OSError as exc:
            print(f"subfleet run: detached launch failed: {exc}", file=sys.stderr)
            run_ledger.finish_run(run_id, rc=127)
            return 127, run_id, None
        run_ledger.update_run(run_id, pid=proc.pid)
        # The detached runner now owns the private temporary prompt and
        # removes it through its EXIT path after ledgering it.
        temp_paths.remove(merged)
        _announce(
            run_id=run_id, pid=proc.pid, model=model, lane=lane_or_home,
            output=output, lane_log=lane_log, reason=mode_reason,
            wait_inline=wait_inline, as_json=args.json,
            notify_target=notify.find_session(caller["session_id"]) if caller else None,
        )
        sys.stdout.flush()
        return 0, run_id, proc.pid

    def wait_for(run_id: str) -> int:
        done = run_ledger.wait_for_runs([run_id])
        meta = done.get(run_id)
        if meta is None:
            return 124
        print(run_ledger.summary_line(run_id, meta, prefix="subfleet run"), file=sys.stderr)
        if meta.get("orphaned") and meta.get("finished_at") is None:
            return 125
        rc = meta.get("rc")
        if rc == 0 and not args.o:
            try:
                sys.stdout.write(Path(run_ledger.output_path(meta) or output).read_text())
            except OSError:
                pass
        return int(rc) if isinstance(rc, int) else 1

    tried: set[tuple[str, str]] = set()
    attempts = 0
    result = 3
    while True:
        lane_or_home: str | None
        if family == "codex":
            candidates = _capacity_candidates(capacity_data, "codex")
            lane_or_home = args.H or reset_picked_home or (
                str(candidates[0]["id"]) if candidates else None
            )
            if not lane_or_home:
                earliest = (capacity_data.get("families", {}).get("codex") or {}).get("earliest_reset")
                print(
                    "delegate: no dispatchable Codex lane"
                    + (f" (earliest reset {earliest})" if earliest else ""),
                    file=sys.stderr,
                )
                result = 3
                cmd: list[str] = []
            else:
                def build_codex_cmd(out_path: str, *, home: str = lane_or_home) -> list[str]:
                    built = [os.environ.get("DELEGATE_CODEX_RUN", _repo_bin("subfleet-codex")), "-H", home,
                             "-m", MODEL_NAMES[model], "-C", args.C, "-p", merged, "-o", out_path, "-s", sandbox]
                    if not args.H:
                        built.append("-A")  # auto-picked: let the runner re-pick on a usage limit
                    if model in CODEX_ULTRA_EFFORT_MODELS: built += ["-e", "ultra"]
                    if args.independent_review:
                        built += ["-I", "-D", str(Path(args.review_root).expanduser().resolve())]
                    if args.b: built += ["-b", args.b]
                    return built

                if args.dry_run:
                    cmd = build_codex_cmd(output)
                    result = 0
                elif mode == "detached":
                    result, run_id, _pid = launch_detached(lane_or_home, build_codex_cmd)
                    cmd = build_codex_cmd(output)
                    if result == 0 and wait_inline and run_id:
                        result = wait_for(run_id)
                else:
                    cmd = build_codex_cmd(output)
                    result = subprocess.run(cmd, env=runner_env(lane_or_home, cmd)).returncode
                if result == 4 and not args.H and not args.dry_run and mode != "detached":
                    # The runner already re-picked across every dispatchable
                    # Codex home (-A) and found them limited. A tier that
                    # starts on a Codex model (Luna) moves upward the way a
                    # Claude tier does, instead of failing the work.
                    next_route = next_semantic_route()
                    if next_route is not None:
                        log_decision(lane_or_home, cmd, result)
                        promote_semantic_model(*next_route, reason="exhausted at runtime")
                        attempts = 0
                        continue
        else:
            model_family = CLAUDE_MODEL_DISPLAY[model]
            full_model = MODEL_NAMES[model]
            tried_for_model = {
                lane for lane, tried_model in tried if tried_model == full_model
            } | {str(email) for email in (getattr(args, "exclude", None) or []) if email}
            candidates = _capacity_candidates(
                capacity_data, "claude", tried_for_model, model_family=model_family
            )
            reserve_drops: list[dict[str, Any]] = []
            reserve_kept: list[dict[str, Any]] = []
            reserve_blocked = False
            if model != "fable":
                reserve_policy = reserve.policy()
                if args.a:
                    reserve_kept, reserve_drops = _reserve_filter(
                        [{"email": args.a, "id": args.a}], model, reserve_policy
                    )
                    reserve_blocked = not reserve_kept
                else:
                    candidates, reserve_drops = _reserve_filter(
                        candidates, model, reserve_policy
                    )
                    reserve_kept = candidates
                    reserve_blocked = bool(reserve_drops) and not candidates
                # Recorded whether or not the reserve blocks this attempt: the
                # kept lanes with their readings sit beside the drops, so the
                # decision log shows why this lane and not another.
                reserve_note = _reserve_record(reserve_kept, reserve_drops, reserve_policy)
            if reserve_blocked:
                # Every lane this model could use still has Fable to protect:
                # move the work upward (Astra when the Codex fleet has room and
                # the task/tier chain allows it, else Fable on these very lanes)
                # rather than spend the shared window under Fable's feet.
                previous_model = model
                upward = next_semantic_route() if not args.a else None
                codex_open = bool(_capacity_candidates(capacity_data, "codex"))
                reason = f"Fable reserve: {reserve.describe(reserve_drops)}"
                reserve_note["blocked_model"] = MODEL_NAMES[previous_model]
                if (
                    upward is not None
                    and MODEL_FAMILY[upward[0]] == "codex"
                    and codex_open
                ):
                    reserve_note["action"] = f"upgraded to {upward[0]}"
                    promote_semantic_model(upward[0], upward[1], reason)
                else:
                    reserve_note["action"] = "upgraded to fable"
                    model, family = "fable", "claude"
                    routing_history.append(
                        {"from": previous_model, "to": "fable", "reason": reason}
                    )
                    routing_note = (
                        f"FABLE RESERVE: {previous_model} would spend Fable on every "
                        f"eligible Claude lane ({reserve.describe(reserve_drops)}); "
                        f"routing {task_class}/{args.tier or 'standard'} upward to fable"
                    )
                    print(f"delegate: WARNING {routing_note}", file=sys.stderr)
                attempts = 0
                continue
            lane_or_home = args.a or (
                str(candidates[0].get("email") or candidates[0]["id"]) if candidates else None
            )
            if not lane_or_home or attempts >= 3:
                if model != "fable":
                    reserve_note["action"] = "no lane" if not lane_or_home else "attempt cap"
                if not lane_or_home and not args.a:
                    scoped_reset = _earliest_scoped_reset(scoped_reasoning or {})
                    print(
                        f"delegate: no dispatchable Claude lane for {model_family}"
                        + (
                            f" (earliest scoped reset {scoped_reset})"
                            if scoped_reset else ""
                        ),
                        file=sys.stderr,
                    )
                cmd = []
                result = 3
            else:
                tried.add((lane_or_home, full_model)); attempts += 1
                if model != "fable":
                    reserve_note["action"] = "dispatched"

                def build_claude_cmd(out_path: str, *, lane: str = lane_or_home,
                                     detached: bool = (mode == "detached")) -> list[str]:
                    built = [os.environ.get("DELEGATE_CLAUDE_LANE", _repo_claude_lane()), "-a", lane,
                             "-m", MODEL_NAMES[model], "-C", args.C, "-p", merged, "-o", out_path, "-s", sandbox]
                    if detached and not args.a:
                        built.append("-A")  # auto-picked: the runner re-picks on a hard limit
                    if args.independent_review:
                        built += ["-I", "-D", str(Path(args.review_root).expanduser().resolve())]
                    if args.b: built += ["-b", args.b]
                    return built

                if args.dry_run:
                    cmd = build_claude_cmd(output)
                    result = 0
                elif mode == "detached":
                    result, run_id, _pid = launch_detached(lane_or_home, build_claude_cmd)
                    cmd = build_claude_cmd(output)
                    if result == 0 and wait_inline and run_id:
                        result = wait_for(run_id)
                else:
                    cmd = build_claude_cmd(output)
                    cp = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        env=runner_env(lane_or_home, cmd),
                    )
                    sys.stdout.write(cp.stdout); sys.stderr.write(cp.stderr)
                    result = cp.returncode
                    if result == 4:
                        explicit_reset = _explicit_limited_until(cp.stderr + cp.stdout)
                        # The instrumented runner records the reset before it
                        # returns. Preserve that precise hook value when the
                        # runner's public output contains only a generic rc=4;
                        # use the conservative fallback only if the hook did
                        # not leave a future cooldown behind.
                        if explicit_reset is not None:
                            record_cooldown(
                                lane_or_home, explicit_reset, model=full_model
                            )
                        elif _future_cooldown(
                            lane_or_home, model=full_model
                        ) is None:
                            record_cooldown(
                                lane_or_home,
                                _limited_until(""),
                                model=full_model,
                            )
                    elif result == 5:
                        record_cooldown(lane_or_home, _now() + timedelta(days=30))
                        print(REENROLL_RITUAL.format(email=lane_or_home), file=sys.stderr)
                    if result == 4 and not args.a:
                        remaining_lanes = _capacity_candidates(
                            capacity_data,
                            "claude",
                            {
                                lane
                                for lane, tried_model in tried
                                if tried_model == full_model
                            },
                            model_family=model_family,
                        )
                        if model != "fable":
                            remaining_lanes, _ = _reserve_filter(remaining_lanes, model)
                        if attempts >= 3 or not remaining_lanes:
                            next_route = next_semantic_route()
                            if next_route is not None:
                                log_decision(lane_or_home, cmd, result)
                                promote_semantic_model(
                                    *next_route,
                                    reason="exhausted at runtime",
                                )
                                attempts = 0
                                continue
                    if result in (4, 5) and not args.a and attempts < 3:
                        log_decision(lane_or_home, cmd, result)
                        continue
                    if result in (4, 5):
                        result = 3

        log_decision(lane_or_home, cmd, result)
        if args.dry_run:
            print(" ".join(__import__("shlex").quote(part) for part in cmd))
        elif result == 0 and mode != "detached" and not args.o and Path(output).exists():
            sys.stdout.write(Path(output).read_text())
        cleanup()
        return result

if __name__ == "__main__":
    raise SystemExit(main())

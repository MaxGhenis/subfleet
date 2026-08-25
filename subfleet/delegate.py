"""Content- and capacity-aware dispatch to the hardened agent runners."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from . import capacity, config, inbox, run_ledger
from .util import atomic_write_json, parse_iso

FABLE_PATTERNS = (
    r"\bvoice\b",
    r"\bemail\b",
    r"\bblog\b",
    r"\bessay\b",
    r"\bprose\b",
    r"\badjudicat\w*\b",
    r"\bverdict\b",
    r"\bfinal review\b",
    r"\bmerge gate\b",
    r"\blaunch\b",
    r"\bsend\b",
    r"\bdesign\b",
    r"\bstrategy\b",
    r"\bwdyt\b",
)
REVIEW_PATTERNS = (
    r"\breview\b",
    r"\bassess\b",
    r"\bcritique\b",
    r"\baudit\b",
    r"\bevaluate\b",
    r"\breferee\b",
)
SWEEP_PATTERNS = (r"for each", r"per-file", r"per-item", r"per-row", r"batch of", r"enumerate", r"across all")
MECHANICAL_PATTERNS = (r"verify", r"count", r"list", r"extract", r"check")
BUILD_PATTERNS = (r"implement", r"fix", r"refactor", r"port", r"migrate", r"wire", r"test")

MODEL_FAMILY = {"fable": "claude", "haiku": "claude", "sol": "codex", "terra": "codex"}
MODEL_NAMES = {
    "fable": "claude-fable-5", "haiku": "claude-haiku-4-5-20251001",
    "sol": "gpt-5.6-sol", "terra": "gpt-5.6-terra",
}
CLAUDE_OVERFLOW_MODEL = {"build": "fable", "review": "fable", "sweep": "haiku"}
PREAMBLE_WRITE = ("Standing orders: commit after every coherent step; create and maintain a committed "
                  "PROGRESS.md (state/done/next) from the start; write your final report to the output file.")
PREAMBLE_AUDIT = "Frame this as a defensive correctness and completeness audit."


def _now() -> datetime:
    return datetime.now().astimezone()


def _state_dir() -> Path:
    return config.state_dir()


def _accounts_file() -> Path:
    return config.accounts_path()


def _discover_tool(name: str, env_var: str) -> str:
    """Resolve an override, this checkout's bin/, then PATH."""
    override = os.environ.get(env_var)
    if override:
        return override
    local = Path(__file__).resolve().parent.parent / "bin" / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name} not found in repository bin or PATH")


def _reenroll_ritual(email: str) -> str:
    secret_name = config.secret_name_for(email, require_enrolled=False)
    return (
        f"Re-enroll lane {email}: run `claude setup-token` while signed into {email}, "
        f"then run `subfleet enroll {email}` (secret item `{secret_name}`)."
    )


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


def choose_model(task_class: str, explicit: str | None = None) -> str:
    return explicit or {
        "fable": "fable",
        "review": "sol",
        "sweep": "terra",
        "build": "sol",
    }[task_class]


def _load_cooldowns() -> dict[str, str]:
    try:
        value = json.loads((_state_dir() / "cooldowns.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cooldowns(data: dict[str, str]) -> None:
    path = _state_dir() / "cooldowns.json"
    atomic_write_json(path, data)


def _rotation() -> dict[str, str]:
    try:
        return json.loads((_state_dir() / "rotation.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _set_last_used(email: str) -> None:
    path = _state_dir() / "rotation.json"
    atomic_write_json(path, {"last_used": email})


def _enrolled() -> list[str]:
    enrolled = config.load().get("enrolled", {})
    return list(enrolled) if isinstance(enrolled, dict) else []


def _active_desktop_email() -> str | None:
    try:
        binary = _discover_tool("subfleet", "DELEGATE_SUBFLEET")
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


def _row_headroom(row: dict[str, Any]) -> float:
    remaining = []
    for key in ("five_hour", "weekly"):
        reading = row.get(key)
        if isinstance(reading, dict) and reading.get("remaining_percent") is not None:
            try:
                remaining.append(float(reading["remaining_percent"]))
            except (TypeError, ValueError):
                pass
    return min(remaining) if remaining else 100.0


def pick_fable_lane(
    exclude: set[str] | None = None,
    capacity_rows: Sequence[dict[str, Any]] | None = None,
) -> str | None:
    """Pick and persist one optimistic Claude lane; the sole replaceable seam."""
    exclude = exclude or set()
    now = _now()
    cooldowns = _load_cooldowns()
    scores = None
    if capacity_rows is not None:
        scores = {
            str(row.get("resource") or row.get("email")): _row_headroom(row)
            for row in capacity_rows
            if row.get("family") == "claude" and row.get("dispatchable")
        }
    live = []
    for email in _enrolled():
        try:
            until = datetime.fromisoformat(cooldowns.get(email, ""))
        except ValueError:
            until = now - timedelta(seconds=1)
        if email not in exclude and until <= now and (scores is None or email in scores):
            live.append(email)
    if not live:
        return None
    if scores is not None:
        best_score = max(scores[email] for email in live)
        live = [email for email in live if scores[email] == best_score]
    last = _rotation().get("last_used")
    if last in live:
        pos = (live.index(last) + 1) % len(live)
        live = live[pos:] + live[:pos]
    active = (
        next(
            (
                str(row.get("email"))
                for row in capacity_rows or []
                if row.get("family") == "claude" and row.get("active") and row.get("email")
            ),
            None,
        )
        if capacity_rows is not None
        else _active_desktop_email()
    )
    if len(live) > 1 and live[0] == active:
        live.append(live.pop(0))
    picked = live[0]
    _set_last_used(picked)
    return picked


def record_cooldown(email: str, until: datetime) -> None:
    capacity.store_lane_cooldown(email, until)


def _earliest_cooldown_reset() -> str | None:
    now = _now()
    enrolled = set(_enrolled())
    resets = []
    for email, raw in _load_cooldowns().items():
        if email not in enrolled:
            continue
        parsed = parse_iso(raw if isinstance(raw, str) else None)
        if parsed is not None and parsed > now:
            resets.append(parsed)
    return min(resets).isoformat() if resets else None


def _limited_until(text: str) -> datetime:
    absolute = re.search(r"hard limit reset=([^\s]+)", text, re.IGNORECASE)
    if absolute:
        parsed = parse_iso(absolute.group(1))
        if parsed is not None and parsed > _now():
            return parsed
    match = re.search(r"resets\s+(\d{1,2}):(\d{2})\s*(am|pm)", text, re.IGNORECASE)
    now = _now()
    if not match:
        return now + timedelta(minutes=60)
    hour, minute, meridiem = int(match[1]), int(match[2]), match[3].lower()
    hour = hour % 12 + (12 if meridiem == "pm" else 0)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def _append_decision(record: dict[str, Any]) -> None:
    path = _state_dir() / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def launch_mode(args: argparse.Namespace, env: dict[str, str] | None = None) -> tuple[str, str]:
    """Decide how the provider process is launched: ('detached'|'sync', why).

    Inside a Claude Code session the tool shell's process tree dies with the
    session (an account switch, or the desktop app stopping an idle session),
    so the provider is launched in its own process session by default and
    `subfleet run` returns immediately; `--attach` keeps the detached launch
    but waits inline. Outside a session (schedulers, a terminal) the
    synchronous runner path is unchanged; `-d` / SUBFLEET_RUN_DETACH=1 detach
    anywhere.
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
    if inbox.in_claude_session(env):
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


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="subfleet run")
    p.add_argument("-t", choices=("fable", "review", "build", "sweep"))
    p.add_argument("-m", choices=("fable", "sol", "terra", "haiku"))
    resources = p.add_mutually_exclusive_group()
    resources.add_argument("-a", metavar="EMAIL")
    resources.add_argument("-H", metavar="CODEX_HOME")
    p.add_argument("-C", default=os.getcwd())
    p.add_argument("-o")
    p.add_argument("-s", choices=("read-only", "workspace-write"))
    p.add_argument("-d", "--detach", dest="d", action="store_true",
                   help="force a detached launch (the default inside a Claude Code session)")
    p.add_argument("--attach", "--wait", dest="attach", action="store_true",
                   help="detached launch, but wait inline for the run to finish")
    p.add_argument("-n", "--name", dest="name",
                   help="short label for the ledger id / run table")
    p.add_argument("--json", action="store_true",
                   help="machine-readable dispatch line for detached launches")
    p.add_argument("-b")
    p.add_argument("--overflow", action="store_true")
    p.add_argument("--no-preamble", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--why", action="store_true")
    p.add_argument("--status", action="store_true")
    source = p.add_mutually_exclusive_group()
    source.add_argument("-p", metavar="PROMPTFILE")
    source.add_argument("prompt", nargs="?")
    return p


def _status() -> int:
    cooldowns, last, now = _load_cooldowns(), _rotation().get("last_used"), _now()
    print(f"Claude lanes (last_used={last or '-'}):")
    for email in _enrolled():
        try:
            until = datetime.fromisoformat(cooldowns.get(email, ""))
        except ValueError:
            until = now
        print(f"  {email}: " + (f"cooldown until {until.isoformat()}" if until > now else "available"))
    try:
        cp = subprocess.run(
            [_discover_tool("subfleet", "DELEGATE_SUBFLEET"), "pick", "codex", "--json", "--all"],
            text=True,
            capture_output=True,
        )
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


def _main(argv: Sequence[str] | None, temp_paths: list[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.status:
        return _status()
    prompt = _prompt_text(args, parser)
    task_class, signals = classify(prompt, args.t)
    model = choose_model(task_class, args.m)
    family = MODEL_FAMILY[model]
    requested_family = family
    if args.a and family != "claude":
        parser.error("-a is only valid with fable or haiku")
    if args.H and family != "codex":
        parser.error("-H is only valid with sol or terra")
    sandbox = args.s or ("workspace-write" if task_class == "build" else "read-only")
    overrides = {k: v for k, v in {"class": args.t, "model": args.m, "lane": args.a, "home": args.H, "sandbox": args.s}.items() if v is not None}

    capacity_error = None
    try:
        capacity_report = capacity.build()
        capacity_rows: list[dict[str, Any]] | None = capacity_report.get("accounts") or []
    except Exception as exc:
        capacity_error = str(exc)
        capacity_rows = None
        capacity_report = {
            "generated_at": _now().isoformat(),
            "cache": {"hit": False, "error": capacity_error},
            "accounts": [],
            "families": {
                "codex": {"score": 0.0, "best_resource": None,
                          "earliest_reset": None, "dispatchable": 0},
                "claude": {"score": 0.0, "best_resource": None,
                           "earliest_reset": None, "dispatchable": 0},
            },
        }
    scores = capacity_report.get("families") or {}
    codex_score = float((scores.get("codex") or {}).get("score") or 0.0)
    claude_score = float((scores.get("claude") or {}).get("score") or 0.0)
    codex_rows = [row for row in (capacity_rows or []) if row.get("family") == "codex"]
    cross_family_note = None
    if (
        args.m is None
        and args.H is None
        and family == "codex"
        and codex_rows
        and codex_score <= 0
        and claude_score > 0
    ):
        model = CLAUDE_OVERFLOW_MODEL[task_class]
        family = "claude"
        cross_family_note = (
            f"CROSS-FAMILY OVERFLOW: {task_class} defaulted to Codex, but its best "
            f"dispatchable headroom is {codex_score:.0f}%; routing to Claude "
            f"({claude_score:.0f}% headroom) as {model}."
        )

    runtime_limited_lanes: list[dict[str, Any]] = []
    capacity_context = {
        "cache": capacity_report.get("cache"),
        "inputs": capacity_report.get("accounts") or [],
        "scores": scores,
        "runtime_limited_lanes": runtime_limited_lanes,
    }
    if capacity_error:
        capacity_context["error"] = capacity_error

    if (
        task_class == "fable"
        and family == "claude"
        and not args.a
        and capacity_rows is not None
        and claude_score <= 0
    ):
        earliest = (scores.get("claude") or {}).get("earliest_reset")
        reason = (
            "FABLE FLOOR BLOCKED: all Claude lanes are limited or unavailable; "
            "refusing to downgrade floor work to Sol"
            + (f" (earliest reset {earliest})" if earliest else "")
        )
        print(f"delegate: {reason}", file=sys.stderr)
        record = {
            "ts": _now().isoformat(), "class": task_class, "model": model,
            "family": family, "requested_family": requested_family,
            "lane/home": None, "signals matched": signals, "overrides": overrides,
            "capacity": capacity_context, "cross_family": None, "reason": reason,
            "result": 3, "cmd": [],
        }
        _append_decision(record)
        if args.why:
            print("delegate decision: " + json.dumps(record, sort_keys=True), file=sys.stderr)
        return 3

    if cross_family_note:
        print(f"delegate: WARNING {cross_family_note}", file=sys.stderr)

    mode, mode_reason = launch_mode(args)
    wait_inline = bool(args.attach) and mode == "detached"
    caller = inbox.caller_context(cwd=args.C)
    output = args.o
    if not output and mode != "detached":
        # Detached runs without -o are hosted by the ledger (run_dir/out.md);
        # synchronous ones still stream through a manufactured temp file.
        fd, output = tempfile.mkstemp(prefix="delegate-output-", suffix=".md")
        temp_paths.append(output)
        os.close(fd)
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
    temp_paths.append(merged)
    with os.fdopen(fd, "w") as f:
        f.write(contents)

    tried: set[str] = set()
    attempts = 0
    result = 3
    reason: str | None = None

    def decision_record(lane_or_home: str | None, cmd: list[str],
                        result: int | None) -> dict[str, Any]:
        return {
            "ts": _now().isoformat(), "class": task_class, "model": model,
            "family": family, "requested_family": requested_family,
            "lane/home": lane_or_home, "signals matched": signals,
            "overrides": overrides, "capacity": capacity_context,
            "cross_family": cross_family_note, "reason": reason,
            "result": result, "cmd": cmd,
        }

    def runner_env(lane_or_home: str, cmd: list[str]) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("SUBFLEET_RUN_LANE_LOG", None)
        env["SUBFLEET_RUN_DECISION_JSON"] = json.dumps(
            decision_record(lane_or_home, cmd, None), sort_keys=True, separators=(",", ":"),
        )
        # Empty means the delegate manufactured a temporary output because the
        # caller did not supply -o; the ledger records that distinction as null.
        env["SUBFLEET_RUN_ORIGINAL_OUT"] = args.o or ""
        env.pop("SUBFLEET_RUN_ID", None)
        if caller is not None:
            env["SUBFLEET_RUN_CALLER_JSON"] = json.dumps(
                caller, sort_keys=True, separators=(",", ":"))
        else:
            env.pop("SUBFLEET_RUN_CALLER_JSON", None)
        return env

    def launch_detached(lane_or_home: str, build_cmd) -> tuple[int, str | None, int | None]:
        """Pre-create the ledger entry, then start the runner in its own
        process session. Returns (result, run_id, pid)."""
        nonlocal output
        slug = _default_slug(args, prompt)
        run_caller = caller
        if wait_inline and caller is not None:
            # This process is the consumer; the completion push is only needed
            # if it dies before the run ends (inbox.on_finish checks the pid).
            run_caller = {**caller, "waiter_pid": os.getpid()}
        stem = output[:-3] if output and output.endswith(".md") else output
        run_id = run_ledger.start_run(
            family=family, model=MODEL_NAMES[model], lane=lane_or_home,
            workdir=args.C, prompt=merged, out=output,
            err=f"{stem}.err.log" if output else None,
            lane_log=f"{stem}.lane.log" if output else None,
            original_out=args.o or "", caller=run_caller, slug=slug,
            launcher="subfleet run",
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
        # The detached runner now owns the temporary prompt and removes it
        # through its exit path after ledgering it.
        if merged in temp_paths:
            temp_paths.remove(merged)
        _announce(
            run_id=run_id, pid=proc.pid, model=model, lane=lane_or_home,
            output=output, lane_log=lane_log, reason=mode_reason,
            wait_inline=wait_inline, as_json=args.json,
            notify_target=inbox.find_session(caller["session_id"]) if caller else None,
        )
        sys.stdout.flush()
        return 0, run_id, proc.pid

    def wait_for(run_id: str) -> int:
        done = run_ledger.wait_for_runs([run_id])
        meta = done.get(run_id)
        if meta is None:
            return 124
        print(run_ledger.summary_line(run_id, meta, prefix="subfleet run"), file=sys.stderr)
        if meta.get("orphaned"):
            return 125
        rc = meta.get("rc")
        if rc == 0 and not args.o:
            try:
                sys.stdout.write(Path(run_ledger.output_path(meta) or output).read_text())
            except OSError:
                pass
        if not isinstance(rc, int):
            return 1
        return rc if rc >= 0 else 1
    while True:
        lane_or_home: str | None
        if family == "codex":
            lane_or_home = args.H or (scores.get("codex") or {}).get("best_resource")
            if not lane_or_home:
                if args.m is None and args.H is None and claude_score > 0:
                    model = CLAUDE_OVERFLOW_MODEL[task_class]
                    family = "claude"
                    cross_family_note = (
                        f"CROSS-FAMILY OVERFLOW: no Codex lane is dispatchable; routing "
                        f"{task_class} to Claude ({claude_score:.0f}% headroom) as {model}."
                    )
                    print(f"delegate: WARNING {cross_family_note}", file=sys.stderr)
                    continue
                reset = (scores.get("codex") or {}).get("earliest_reset")
                print(
                    "delegate: no dispatchable Codex lane"
                    + (f" (earliest reset {reset})" if reset else ""),
                    file=sys.stderr,
                )
                result = 3
                cmd: list[str] = []
            else:
                try:
                    runner = _discover_tool("subfleet-codex", "DELEGATE_CODEX_RUN")
                except FileNotFoundError as exc:
                    print(f"delegate: {exc}", file=sys.stderr)
                    cmd = []
                    result = 3
                else:
                    def build_codex_cmd(out_path: str, *, home: str = lane_or_home,
                                        tool: str = runner) -> list[str]:
                        built = [tool, "-H", home, "-m", MODEL_NAMES[model],
                                 "-C", args.C, "-p", merged, "-o", out_path, "-s", sandbox]
                        if not args.H:
                            built.append("-A")  # auto-picked: the runner re-picks on a usage limit
                        if model == "sol": built += ["-e", "ultra"]
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
        else:
            lane_or_home = args.a or pick_fable_lane(tried, capacity_rows)
            if not lane_or_home or attempts >= 3:
                cmd = []
                result = 3
                if task_class == "fable":
                    earliest = _earliest_cooldown_reset() or \
                        (scores.get("claude") or {}).get("earliest_reset")
                    if attempts >= 3:
                        reason = (
                            f"FABLE FLOOR STOPPED: Claude retry cap reached after {attempts} "
                            "unavailable lane attempts; refusing any Sol downgrade"
                        )
                    else:
                        reason = (
                            "FABLE FLOOR BLOCKED: no dispatchable Claude lane remains; "
                            "refusing any Sol downgrade"
                        )
                    if earliest:
                        reason += f" (earliest reset {earliest})"
            else:
                tried.add(lane_or_home); attempts += 1
                try:
                    runner = _discover_tool("subfleet-claude", "DELEGATE_CLAUDE_LANE")
                except FileNotFoundError as exc:
                    print(f"delegate: {exc}", file=sys.stderr)
                    cmd = []
                    result = 3
                else:
                    def build_claude_cmd(out_path: str, *, lane: str = lane_or_home,
                                         tool: str = runner) -> list[str]:
                        built = [tool, "-a", lane, "-m", MODEL_NAMES[model],
                                 "-C", args.C, "-p", merged, "-o", out_path, "-s", sandbox]
                        if mode == "detached" and not args.a:
                            built.append("-A")  # auto-picked: the runner re-picks on a hard limit
                        if args.b: built += ["-b", args.b]
                        return built

                    if mode == "detached":
                        result, run_id, _pid = launch_detached(lane_or_home, build_claude_cmd)
                        cmd = build_claude_cmd(output)
                        if result == 0 and wait_inline and run_id:
                            result = wait_for(run_id)
                    else:
                        cmd = build_claude_cmd(output)
                        cp = None if args.dry_run else subprocess.run(
                            cmd, capture_output=True, text=True,
                            env=runner_env(lane_or_home, cmd))
                        if cp is None:
                            result = 0
                        else:
                            sys.stdout.write(cp.stdout); sys.stderr.write(cp.stderr)
                            result = cp.returncode
                            cooldown_until = None
                            if result == 4:
                                cooldown_until = _limited_until(cp.stderr + cp.stdout)
                                record_cooldown(lane_or_home, cooldown_until)
                            elif result == 5:
                                cooldown_until = _now() + timedelta(days=30)
                                record_cooldown(lane_or_home, cooldown_until)
                                print(_reenroll_ritual(lane_or_home), file=sys.stderr)
                            if result in (4, 5):
                                runtime_limited_lanes.append(
                                    {
                                        "resource": lane_or_home,
                                        "email": lane_or_home,
                                        "result": result,
                                        "outcome": "hard_limit" if result == 4 else "auth_failure",
                                        "limited_until": cooldown_until.isoformat(),
                                    }
                                )
                            if result in (4, 5) and not args.a and attempts < 3:
                                attempt_record = {
                                    "ts": _now().isoformat(), "class": task_class, "model": model,
                                    "family": family, "requested_family": requested_family,
                                    "lane/home": lane_or_home, "signals matched": signals,
                                    "overrides": overrides, "capacity": capacity_context,
                                    "cross_family": cross_family_note, "result": result, "cmd": cmd,
                                }
                                _append_decision(attempt_record)
                                if args.why:
                                    print("delegate decision: " + json.dumps(attempt_record, sort_keys=True), file=sys.stderr)
                                continue
                            if result in (4, 5):
                                runtime_result = result
                                result = 3
                                if task_class == "fable":
                                    earliest = _earliest_cooldown_reset() or \
                                        (scores.get("claude") or {}).get("earliest_reset")
                                    if args.a:
                                        reason = (
                                            f"FABLE FLOOR STOPPED: pinned Claude lane became "
                                            f"unavailable (rc {runtime_result}); refusing any Sol downgrade"
                                        )
                                    else:
                                        reason = (
                                            f"FABLE FLOOR STOPPED: Claude retry cap reached after "
                                            f"{attempts} unavailable lane attempts; refusing any Sol downgrade"
                                        )
                                    if earliest:
                                        reason += f" (earliest reset {earliest})"

        if reason:
            print(f"delegate: {reason}", file=sys.stderr)
        record = {
            "ts": _now().isoformat(), "class": task_class, "model": model,
            "family": family, "requested_family": requested_family,
            "lane/home": lane_or_home, "signals matched": signals, "overrides": overrides,
            "capacity": capacity_context, "cross_family": cross_family_note,
            "reason": reason, "result": result, "cmd": cmd,
        }
        _append_decision(record)
        if args.why:
            print("delegate decision: " + json.dumps(record, sort_keys=True), file=sys.stderr)
        if args.dry_run:
            print(" ".join(__import__("shlex").quote(part) for part in cmd))
        elif result == 0 and mode != "detached" and not args.o and Path(output).exists():
            sys.stdout.write(Path(output).read_text())
        return result


def main(argv: Sequence[str] | None = None) -> int:
    temp_paths: list[str] = []
    try:
        return _main(argv, temp_paths)
    finally:
        for path in temp_paths:
            try:
                Path(path).unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Aggregate, metadata-only statistics for native-workflows-vs-subfleet.md,
subfleet v2 edition (measured 2026-09-27). Read-only. Stdlib only.

Sources:
  1. Claude Code native workflow state files,
     ~/.claude/projects/*/*/workflows/wf_*.json, plus the first line of one
     worker transcript per run (Claude Code version and entrypoint only).
  2. Runs of the saved `subfleet-fanout` workflow: the same state files, plus
     the dispatcher transcripts (Bash call timings and the WAIT_RC/SUBMIT_RC
     markers the script prints; nothing else).
  3. The subfleet v2 store, ~/.subfleet/state.sqlite3, opened with mode=ro.
  4. With --live: `subfleet status --json`, a read-only daemon query.

Never prints prompts, outputs or verdict text. Account e-mail addresses are
stripped from everything printed; lanes are named by lane id.

Usage: native-workflows-vs-subfleet.ledger-stats.py [--since ISO] [--live]
The default --since is the previous measurement, 2026-09-17T19:55:00Z
(15:55 EDT, when the v1 edition of the doc counted 707 state files).
"""
import argparse
import collections
import glob
import json
import os
import re
import sqlite3
import statistics
import subprocess
from datetime import datetime, timezone

C = collections.Counter
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")
HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
STORE = os.path.join(HOME, ".subfleet", "state.sqlite3")
BASH_CAP_S = 600


def row(metric, value):
    print(f"{metric} :: {EMAIL.sub('<email>', str(value))}")


def pct(values, q):
    """Linear-interpolated percentile (numpy's default)."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt_s(x):
    return "-" if x is None else f"{x:,.0f}s ({x / 60:.1f}m)"


def spread(values, fmt=fmt_s):
    xs = [v for v in values if v is not None]
    if not xs:
        return "n=0"
    return (f"n={len(xs)} p10={fmt(pct(xs, 10))} p50={fmt(pct(xs, 50))} "
            f"p90={fmt(pct(xs, 90))} max={fmt(max(xs))}")


def dist(counter):
    total = sum(counter.values()) or 1
    return ", ".join(f"{k}={v} ({100 * v / total:.1f}%)" for k, v in counter.most_common())


def iso(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def max_overlap(intervals):
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], e[1]))  # ends before starts on ties
    cur = best = 0
    for _, d in events:
        cur += d
        best = max(best, cur)
    return best


def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except ValueError:
        return None


# Worker error text is matched with \s+ patterns and reported under hyphenated
# labels, so this file and its output never carry the plain provider phrases
# that subfleet v2's Claude classifier reads as a lane fault (see the doc,
# "Where subfleet v2 breaks", item 1).
ERROR_CLASSES = (
    ("session-limit", re.compile(r"session\s+limit", re.I)),
    ("weekly-limit", re.compile(r"weekly\s+limit", re.I)),
    ("other plan-limit or rate-limit", re.compile(r"hit\s+your|usage\s+limit|rate[\s_]+limit", re.I)),
)


def error_class(text):
    for label, pattern in ERROR_CLASSES:
        if pattern.search(text or ""):
            return label
    return "other"


# ------------------------------------------------------------ native workflows
def load_state_files():
    runs, bad = [], 0
    for path in glob.glob(os.path.join(PROJECTS, "*", "*", "workflows", "wf_*.json")):
        try:
            with open(path) as fh:
                runs.append((path, json.load(fh)))
        except (OSError, ValueError):
            bad += 1
    return runs, bad


def agents_of(state):
    return [p for p in state.get("workflowProgress") or [] if p.get("type") == "workflow_agent"]


def run_transcript_dir(path, state):
    session_dir = os.path.dirname(os.path.dirname(path))
    return os.path.join(session_dir, "subagents", "workflows", state.get("runId", ""))


def first_transcript_header(path, state):
    """Claude Code version and entrypoint from one worker transcript line."""
    for f in sorted(glob.glob(os.path.join(run_transcript_dir(path, state), "agent-*.jsonl"))):
        try:
            with open(f) as fh:
                for line in fh:
                    rec = json.loads(line)
                    if rec.get("version"):
                        return rec.get("version"), rec.get("entrypoint")
        except (OSError, ValueError):
            continue
    return None, None


def native_section(runs, bad, since_ms):
    print("=== NATIVE WORKFLOWS ===")
    row("state files (all time) / unreadable", f"{len(runs)} / {bad}")
    window = [(p, s) for p, s in runs if (s.get("startTime") or 0) >= since_ms]
    row("runs started in window", len(window))
    starts = [s["startTime"] for _, s in window]
    if starts:
        first = datetime.fromtimestamp(min(starts) / 1000, timezone.utc)
        last = datetime.fromtimestamp(max(starts) / 1000, timezone.utc)
        row("window first/last start (UTC)", f"{first:%Y-%m-%dT%H:%MZ} .. {last:%Y-%m-%dT%H:%MZ}")
    row("status", dist(C(s.get("status") for _, s in window)))
    per_run = [len(agents_of(s)) for _, s in window]
    row("agents (sum)", sum(per_run))
    row("agents per run p50/p90/max", f"{pct(per_run, 50):.0f} / {pct(per_run, 90):.0f} / {max(per_run)}")
    row("tokens (sum of run totalTokens)", f"{sum(s.get('totalTokens') or 0 for _, s in window):,}")
    models = C(a.get("model") for _, s in window for a in agents_of(s))
    row("agent models", dist(models))
    aliases = {"opus", "sonnet", "haiku", "fable"}
    row("model aliases recorded as-is", sorted(m for m in models if m in aliases) or "none")
    row("non-Anthropic model ids", sorted(m for m in models if m and m not in aliases
                                          and not str(m).startswith("claude-")) or "none")
    row("agent state", dist(C(a.get("state") for _, s in window for a in agents_of(s))))

    errors = [(p, s, a) for p, s in window for a in agents_of(s) if a.get("state") == "error"]
    row("worker errors", len(errors))
    row("worker errors by class", dist(C(error_class(a.get("error")) for _, _, a in errors)))
    limited_runs = {}
    for p, s, a in errors:
        if error_class(a.get("error")) != "other":
            limited_runs.setdefault(s["runId"], (p, s))
    row("runs with >=1 limit-class worker error", len(limited_runs))
    row("  of which status", dist(C(s.get("status") for _, s in limited_runs.values())))
    worst = None
    for _, s in limited_runs.values():
        if s.get("status") != "completed":
            continue
        ags = agents_of(s)
        n_err = sum(1 for a in ags if a.get("state") == "error")
        if worst is None or n_err / len(ags) > worst[0] / worst[1]:
            worst = (n_err, len(ags), s["runId"])
    if worst:
        row("completed run with the largest errored share", f"{worst[0]} of {worst[1]} agents errored ({worst[2]})")

    # Peak concurrency per run, from agent start and duration.
    peaks, timed_big = [], []
    for _, s in window:
        iv = []
        for a in agents_of(s):
            st, du = a.get("startedAt"), a.get("durationMs")
            if st is None:
                continue
            end = st + du if du is not None else a.get("lastProgressAt")
            if end is not None:
                iv.append((st, end))
        if iv:
            peak = max_overlap(iv)
            peaks.append(peak)
            if len(iv) >= 30:
                timed_big.append(peak)
    row("peak in-flight agents per run, max", max(peaks) if peaks else "-")
    row("runs with >=30 timed agents / peaking at exactly 16",
        f"{len(timed_big)} / {sum(1 for x in timed_big if x == 16)}; distinct peaks {dict(C(timed_big))}")

    # Claude Code version and entrypoint of each limited run (worker transcript header).
    by_ver = C()
    by_entry = C()
    sl_new = 0
    for p, s, a in errors:
        if error_class(a.get("error")) != "session-limit":
            continue
        ver, entry = first_transcript_header(p, s)
        by_ver[ver] += 1
        by_entry[entry] += 1
        vt = version_tuple(ver) if ver else None
        if vt and vt >= (2, 1, 271):
            sl_new += 1
    row("session-limit worker errors on Claude Code >= 2.1.271", sl_new)
    row("session-limit worker errors by worker entrypoint", dist(by_entry))
    row("session-limit worker errors by Claude Code version (top 8)",
        ", ".join(f"{k}={v}" for k, v in by_ver.most_common(8)))
    return window


# ------------------------------------------------------------ subfleet-fanout
def is_fanout(state):
    return state.get("workflowName") == "subfleet-fanout" or "name: 'subfleet-fanout'" in (state.get("script") or "")


def bash_calls(path, state):
    """(duration_s, markers) per Bash call in the dispatcher transcripts."""
    calls = []
    for f in sorted(glob.glob(os.path.join(run_transcript_dir(path, state), "agent-*.jsonl"))):
        uses = {}
        try:
            with open(f) as fh:
                for line in fh:
                    rec = json.loads(line)
                    ts = iso(rec.get("timestamp"))
                    content = (rec.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if block.get("type") == "tool_use" and block.get("name") == "Bash":
                            uses[block.get("id")] = ts
                        elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                            text = block.get("content")
                            if isinstance(text, list):
                                text = " ".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
                            markers = re.findall(r"(WAIT_RC=\d+|SUBMIT_RC=\d+)", str(text))
                            start = uses[block.get("tool_use_id")]
                            dur = (ts - start).total_seconds() if ts and start else None
                            calls.append((dur, markers))
        except (OSError, ValueError):
            continue
    return calls


def fanout_section(runs):
    print("\n=== SUBFLEET-FANOUT RUNS ===")
    fan = sorted(((p, s) for p, s in runs if is_fanout(s)), key=lambda x: x[1].get("startTime") or 0)
    row("runs", len(fan))
    row("status", dist(C(s.get("status") for _, s in fan)))
    tokens, durations = [], []
    for p, s in fan:
        ags = agents_of(s)
        shape = "batch" if "--batch" in (s.get("script") or "") else "per-item"
        res = s.get("result")
        summary = {}
        if isinstance(res, dict):
            summary = {k: res.get(k) for k in ("submit_rc", "wait_loops", "last_wait_rc")}
            summary["jobs"] = dict(C(j.get("state") for j in res.get("jobs") or []))
        elif isinstance(res, list):
            summary = {"jobs": dict(C("rc=%s" % j.get("rc") for j in res)),
                       "wait_loops": [j.get("wait_loops") for j in res]}
        calls = bash_calls(p, s)
        durs = [d for d, _ in calls if d is not None]
        markers = C(m.split("=")[0] + "=" + m.split("=")[1] for _, ms in calls for m in ms)
        tokens.append(s.get("totalTokens") or 0)
        durations.append((s.get("durationMs") or 0) / 1000)
        start = datetime.fromtimestamp((s.get("startTime") or 0) / 1000, timezone.utc)
        row(f"run {s.get('runId')}",
            f"{start:%Y-%m-%d} {shape} status={s.get('status')} dur={(s.get('durationMs') or 0) / 1000:,.0f}s "
            f"dispatchers={len(ags)} models={sorted({a.get('model') for a in ags})} "
            f"login_tokens={s.get('totalTokens'):,} bash_calls={len(calls)} "
            f"longest_bash={max(durs) if durs else 0:,.0f}s over_{BASH_CAP_S}s={sum(1 for d in durs if d > BASH_CAP_S)} "
            f"markers={dict(markers)} result={summary}")
    row("login tokens per run min/p50/max", f"{min(tokens):,} / {statistics.median(tokens):,.0f} / {max(tokens):,}")
    row("run durations", spread(durations))
    row(f"runs longer than {BASH_CAP_S}s", sum(1 for d in durations if d > BASH_CAP_S))


# ------------------------------------------------------------ subfleet v2 store
def store_section(since):
    print("\n=== SUBFLEET V2 STORE ===")
    if not os.path.exists(STORE):
        row("store", "missing")
        return
    row("store bytes (main file)", f"{os.path.getsize(STORE):,}")
    con = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    q = lambda sql, *a: con.execute(sql, a).fetchall()
    for t in ("lanes", "jobs", "attempts", "notices", "service_notices", "decisions", "events",
              "closures", "readings", "artifacts"):
        row(f"rows.{t}", q(f"select count(*) from {t}")[0][0])
    jobs = [dict(r) for r in q("select * from jobs")]
    row("jobs created_at min/max (retention keeps a window)",
        f"{min(j['created_at'] for j in jobs)} .. {max(j['created_at'] for j in jobs)}")
    row("jobs kind x state", dist(C(f"{j['kind']}/{j['state']}" for j in jobs)))
    dispatch = [j for j in jobs if j["kind"] == "dispatch"]
    row("dispatch jobs routed by", dist(C(
        ("task+tier" if j["task"] else "no task") + (" +pin" if j["pinned_model"] else "")
        + (" +lane" if j["pinned_lane"] else "") for j in dispatch)))
    row("dispatch task/tier -> pin", dist(C(f"{j['task']}/{j['tier']}/{j['pinned_model']}" for j in dispatch)))
    row("dispatch sandbox", dist(C(j["sandbox"] for j in dispatch)))
    row("dispatch sandbox x worktree column set x in_place", dist(C(
        f"{j['sandbox']}/worktree={'set' if j['worktree'] else 'null'}/in_place={j['in_place']}" for j in dispatch)))
    row("jobs with parent / independent / allow_desktop",
        f"{sum(1 for j in jobs if j['parent_job_id'])} / {sum(1 for j in jobs if j['independent'])} / "
        f"{sum(1 for j in jobs if j['allow_desktop'])}")
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    row("dispatch request ids: default UUID4 / caller-chosen",
        f"{sum(1 for j in dispatch if uuid_re.match(j['request_id']))} / "
        f"{sum(1 for j in dispatch if not uuid_re.match(j['request_id']))}")
    row("jobs max_attempts / max_wall_s", f"{dict(C(j['max_attempts'] for j in jobs))} / {dict(C(j['max_wall_s'] for j in jobs))}")
    row("jobs max_tokens_observed non-null", sum(1 for j in jobs if j["max_tokens_observed"] is not None))

    # Batches, from job.submitted events.
    batch = C()
    for r in q("select data_json from events where kind='job.submitted'"):
        try:
            b = (json.loads(r[0]) or {}).get("batch")
        except ValueError:
            b = None
        if isinstance(b, dict):
            batch[b.get("id")] += 1
    row("job.submitted events / distinct jobs / still in the jobs table",
        f"{q('select count(*) from events where kind=?', 'job.submitted')[0][0]} / "
        f"{q('select count(distinct job_id) from events where kind=?', 'job.submitted')[0][0]} / "
        f"{q('select count(*) from jobs j where exists (select 1 from events e where e.kind=? and e.job_id=j.job_id)', 'job.submitted')[0][0]}")
    row("job.submitted with batch / distinct batches", f"{sum(batch.values())} / {len(batch)}")
    if batch:
        row("batch sizes (submitted entries)", sorted(batch.values(), reverse=True))

    # Durations and queue waits.
    fin = [j for j in dispatch if j["started_at"] and j["finished_at"]]
    row("dispatch run time (started->finished), succeeded",
        spread([(iso(j["finished_at"]) - iso(j["started_at"])).total_seconds() for j in fin if j["state"] == "succeeded"]))
    row("dispatch queue wait (created->started), all started",
        spread([(iso(j["started_at"]) - iso(j["created_at"])).total_seconds() for j in dispatch if j["started_at"]]))
    # Events outlive pruned job rows, so the complete queue-wait record is
    # job.submitted -> first attempt.starting (or job.cancel_requested for a job
    # cancelled before any attempt started). Includes gate rounds and turns.
    first = {}
    for kind in ("job.submitted", "attempt.starting", "job.cancel_requested"):
        for r in q("select job_id, min(ts) from events where kind=? and job_id is not null group by job_id", kind):
            first.setdefault(r[0], {})[kind] = iso(r[1])
    waits = [(v["attempt.starting"] - v["job.submitted"]).total_seconds()
             for v in first.values() if "job.submitted" in v and "attempt.starting" in v]
    cancelled_first = [(v["job.cancel_requested"] - v["job.submitted"]).total_seconds()
                       for v in first.values() if "job.submitted" in v and "attempt.starting" not in v
                       and "job.cancel_requested" in v]
    row("time to first attempt start (events, all kinds)", spread(waits))
    row("cancelled before any attempt started: wait until cancel (events)", spread(cancelled_first))
    now = datetime.now(timezone.utc)
    queued = [j for j in dispatch if j["state"] == "queued"]
    row("queued now: count / age", f"{len(queued)} / " + spread([(now - iso(j['created_at'])).total_seconds() for j in queued]))

    atts = [dict(r) for r in q("select * from attempts")]
    row("attempts state x outcome_class", dist(C(f"{a['state']}/{a['outcome_class']}" for a in atts)))
    row("attempts model_requested", dist(C(a["model_requested"] for a in atts)))
    row("attempts attestation", dist(C(a["attestation"] for a in atts)))
    per_job = C(a["job_id"] for a in atts)
    row("attempts per job", dict(C(per_job.values())))

    by_model = collections.defaultdict(list)
    for a in atts:
        if a["state"] == "succeeded" and a["started_at"] and a["finished_at"]:
            by_model[a["model_requested"]].append((iso(a["finished_at"]) - iso(a["started_at"])).total_seconds())
    for model, ds in sorted(by_model.items(), key=lambda kv: -len(kv[1])):
        row(f"succeeded attempt run time {model}", spread(ds))

    live_states = {"starting", "running", "finalizing"}

    def span(a):
        if not a["started_at"]:
            return None
        if a["finished_at"]:
            return iso(a["started_at"]), iso(a["finished_at"])
        return (iso(a["started_at"]), now) if a["state"] in live_states else None

    iv = [s for s in (span(a) for a in atts) if s]
    row("max concurrently running attempts (retained rows)", max_overlap(iv))
    row("attempts with no finished_at by state", dict(C(a["state"] for a in atts if not a["finished_at"])))
    kind_of = {j["job_id"]: j["kind"] for j in jobs}
    lanes_iv = collections.defaultdict(list)
    lanes_iv_jobs = collections.defaultdict(list)
    for a in atts:
        s = span(a)
        if s:
            lanes_iv[a["lane_id"]].append(s)
            if kind_of.get(a["job_id"]) != "turn":
                lanes_iv_jobs[a["lane_id"]].append(s)
    row("max concurrent attempts per lane, turns included (top)",
        dict(sorted(((k, max_overlap(v)) for k, v in lanes_iv.items()), key=lambda kv: -kv[1])[:8]))
    row("max concurrent attempts per lane, conversation turns excluded",
        max(max_overlap(v) for v in lanes_iv_jobs.values()) if lanes_iv_jobs else 0)

    sessions = collections.defaultdict(list)
    for j in dispatch:
        if j["caller_session"] and j["started_at"]:
            sessions[j["caller_session"]].append((iso(j["started_at"]), iso(j["finished_at"]) or now))
    peaks = sorted(max_overlap(v) for v in sessions.values())
    if peaks:
        row("caller sessions / per-session peak running jobs p50/max", f"{len(peaks)} / {pct(peaks, 50):.0f} / {peaks[-1]}")

    row("notices state x transport", dist(C(f"{r['state']}/{r['transport']}" for r in q("select state, transport from notices"))))
    row("service_notices state", dist(C(r[0] for r in q("select state from service_notices"))))
    fin_called = [j for j in jobs if j["caller_session"] and j["finished_at"]]
    notice_state = {}
    for r in q("select job_id, state from notices where job_id is not null"):
        rank = {"pending": 0, "offered": 1, "acknowledged": 2, "surfaced": 3}
        if rank.get(r["state"], -1) > rank.get(notice_state.get(r["job_id"]), -1):
            notice_state[r["job_id"]] = r["state"]
    row("finished caller jobs: best notice state", dist(C(notice_state.get(j["job_id"], "no notice") for j in fin_called)))

    row("closures reason/scope (all retained)", dist(C(f"{r['reason']}/{r['scope']}" for r in q("select reason, scope from closures"))))
    for kind in ("job.wall_limit", "attempt.quarantined", "retention.pruned", "admission.recovered", "gate.state"):
        row(f"events.{kind}", q("select count(*) from events where kind=?", kind)[0][0])
    gates = C((j["round_lease"] or "").split(":round:")[0] for j in jobs if j["kind"] == "gate-review")
    row("gate-review jobs / distinct gates / rounds per gate p50/max",
        f"{sum(gates.values())} / {len(gates)} / {pct(list(gates.values()), 50):.0f} / {max(gates.values()) if gates else 0}")
    lanes = [dict(r) for r in q("select lane_id, provider, enabled, desktop, identity_status from lanes")]
    row("lanes provider/enabled/desktop", dist(C(f"{l['provider']}/enabled={l['enabled']}/desktop={l['desktop']}" for l in lanes)))


# ------------------------------------------------------------ live capacity
def live_section():
    print("\n=== LIVE CAPACITY (subfleet status --json) ===")
    try:
        out = subprocess.run(["subfleet", "status", "--json"], capture_output=True, text=True, timeout=60)
        st = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        row("status", f"unavailable: {type(exc).__name__}")
        return
    row("now", st.get("now"))
    adm = st.get("admission") or {}
    row("admission", {k: adm.get(k) for k in ("pending", "open_lanes", "reasons", "idle_for_s")})
    active = collections.defaultdict(list)
    for c in st.get("closures") or []:
        if not c.get("released_at"):
            active[c["lane_id"]].append(f"{c['reason']}/{c['scope']} until {c['until_at']}")
    classes = collections.defaultdict(list)
    for lane in (st.get("lanes") or []):
        lid, prov = lane["lane_id"], lane.get("provider")
        if not lane.get("enabled"):
            cls = "disabled"
        elif lane.get("desktop"):
            cls = "desktop (excluded unless --allow-desktop)"
        elif active.get(lid):
            cls = "closed: " + "; ".join(sorted({x.split(" until")[0] for x in active[lid]}))
        else:
            cls = (f"enabled, no closure, in_flight={lane.get('in_flight')}, "
                   f"dispatchable={lane.get('dispatchable')}, measured={lane.get('measured')}")
        classes[(prov, cls)].append(lid)
    for (prov, cls), ids in sorted(classes.items()):
        row(f"{prov} lanes {cls}", f"{len(ids)}: {', '.join(sorted(ids))}")
    row("in_flight by lane", st.get("in_flight"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-17T19:55:00Z")
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    since = iso(a.since)
    runs, bad = load_state_files()
    native_section(runs, bad, since.timestamp() * 1000)
    fanout_section(runs)
    store_section(since)
    if a.live:
        live_section()


if __name__ == "__main__":
    main()

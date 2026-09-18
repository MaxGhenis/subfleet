#!/usr/bin/env python3
"""Aggregate, metadata-only statistics over the subfleet run ledger, gate
state, and the delegate decision log. Read-only. Stdlib only.

Never reads prompt.md / out.md / err.log / brief.md / verdict text. Lane and
account identifiers are reduced to distinct counts; routing reasons are
reduced to a category with any e-mail address stripped.
"""
import collections
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta

STATE = os.path.expanduser("~/chief-of-staff/state/subfleet")
RUNS = os.path.join(STATE, "runs")
GATES = os.path.join(STATE, "gates")
DECISIONS = [
    os.path.join(STATE, "decisions.jsonl"),
    os.path.expanduser("~/.local/state/delegate/decisions.jsonl"),  # delegate._state_dir() default
]
C = collections.Counter


def ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def pct(values, q):
    """Linear-interpolated percentile (numpy 'linear' default)."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt_s(x):
    if x is None:
        return "-"
    return f"{x:,.0f}s ({x/60:.1f}m)"


def dist(counter, total=None):
    total = total if total is not None else sum(counter.values())
    return ", ".join(f"{k}={v} ({100*v/total:.1f}%)" for k, v in counter.most_common())


def row(metric, value):
    print(f"{metric} :: {value}")


def max_overlap(intervals):
    events = []
    for s, e in intervals:
        events.append((s, 1))
        events.append((e, -1))
    events.sort(key=lambda x: (x[0], x[1]))  # ends before starts on ties
    cur = best = 0
    for _, d in events:
        cur += d
        best = max(best, cur)
    return best


def max_burst(starts, window_s):
    xs = sorted(starts)
    best = j = 0
    for i, s in enumerate(xs):
        while s - xs[j] > timedelta(seconds=window_s):
            j += 1
        best = max(best, i - j + 1)
    return best


EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")


def reason_category(reason):
    r = EMAIL.sub("<lane>", str(reason or ""))
    if r.startswith("Fable reserve"):
        m = re.search(r"unmeasured \(([a-z-]+)\)", r)
        if m:
            return f"fable-reserve: lane unmeasured ({m.group(1)})"
        if "slack" in r:
            return "fable-reserve: slack below minimum"
        return "fable-reserve: other"
    if "exhausted at runtime" in r:
        return "exhausted at runtime (quota hit mid-dispatch)"
    if r == "capacity snapshot":
        return "capacity snapshot (no dispatchable capacity for first choice)"
    return "other"


def notify_state(meta):
    """Replica of run_ledger._notify_state."""
    if not isinstance(meta.get("caller"), dict):
        return "no-caller (not dispatched from a Claude session)"
    info = meta.get("notify")
    if meta.get("finished_at") is None:
        return "pending"
    if not isinstance(info, dict):
        return "none"
    followup = info.get("followup") if isinstance(info.get("followup"), dict) else {}
    if info.get("surfaced"):
        return "landed" if info.get("pushed") and info.get("surfaced_by") == "transcript" else "surfaced"
    if followup.get("revive"):
        return "revived"
    if followup.get("lost"):
        return "lost"
    if info.get("pushed"):
        return "pushed"
    return "parked"


# ---------------------------------------------------------------- runs ledger
metas = []
for name in sorted(os.listdir(RUNS)):
    path = os.path.join(RUNS, name, "meta.json")
    if os.path.isfile(path):
        try:
            metas.append(json.load(open(path)))
        except (OSError, ValueError):
            pass
N = len(metas)
print("=== RUN LEDGER ===")
row("runs.count", N)
starts = [ts(m.get("started_at")) for m in metas]
starts_ok = [s for s in starts if s]
finished = [m for m in metas if m.get("finished_at") is not None]
fin_starts = [ts(m["started_at"]) for m in finished if ts(m.get("started_at"))]
row("runs.started_at.min/max (all)", f"{min(starts_ok).isoformat()} .. {max(starts_ok).isoformat()}")
row("runs.started_at.min/max (finished only)", f"{min(fin_starts).isoformat()} .. {max(fin_starts).isoformat()}")
row("runs.unfinished (finished_at null; never pruned)", N - len(finished))
row("runs.orphaned flag", sum(1 for m in metas if m.get("orphaned")))
per_day = C(s.date().isoformat() for s in fin_starts)
row("runs.per_active_day (finished) n_days/p50/max", f"{len(per_day)} / {pct(per_day.values(),50):.0f} / {max(per_day.values())}")
row("runs.per_day.detail", ", ".join(f"{d}:{c}" for d, c in sorted(per_day.items())))

row("runs.by_family", dist(C(m.get("family") for m in metas)))
row("runs.by_model", dist(C(m.get("model") for m in metas)))
row("runs.distinct_lanes (count only)", f"all={len({m.get('lane') for m in metas if m.get('lane')})}; "
    + "; ".join(f"{fam}={len({m.get('lane') for m in metas if m.get('family')==fam and m.get('lane')})}" for fam in ("claude", "codex")))
row("runs.distinct_workdirs (count only)", len({m.get("workdir") for m in metas}))

rds = [m.get("routing_decision") for m in metas]
rd_full = [r for r in rds if isinstance(r, dict) and "class" in r]
rd_resume = [r for r in rds if isinstance(r, dict) and r.get("kind") == "codex-resume"]
row("routing_decision.present", f"full={len(rd_full)}, codex-resume stub={len(rd_resume)}, null={sum(1 for r in rds if r is None)}")
row("routing_decision.top_level_keys", sorted({k for r in rd_full for k in r}))
row("routing.by_task", dist(C(r.get("task") for r in rd_full)))
row("routing.by_tier", dist(C(r.get("tier") for r in rd_full)))
row("routing.by_class (always set; inferred from signals when no --task)", dist(C(r.get("class") for r in rd_full)))
tt = C((r.get("task"), r.get("tier"), r.get("model")) for r in rd_full if r.get("task"))
row("routing.task x tier -> model", "; ".join(f"{t}/{ti}->{mo}={c}" for (t, ti, mo), c in tt.most_common()))


def how(r):
    ov = r.get("overrides") or {}
    parts = []
    if ov.get("task"):
        parts.append("task+tier")
    if ov.get("model"):
        parts.append("-m model pin")
    if ov.get("lane") or ov.get("home"):
        parts.append("lane pin")
    return " & ".join(parts) or "no task/model/lane flag (signal-inferred class)"


row("routing.selection_inputs (from overrides)", dist(C(how(r) for r in rd_full)))
row("routing.overrides.sandbox", dist(C((r.get("overrides") or {}).get("sandbox") for r in rd_full)))
row("routing.overrides.independent_review", sum(1 for r in rd_full if (r.get("overrides") or {}).get("independent_review")))
row("routing.requested_model != model", sum(1 for r in rd_full if r.get("requested_model") != r.get("model")))
row("routing.requested->actual where changed", dist(C(f"{r.get('requested_model')}->{r.get('model')}" for r in rd_full if r.get("requested_model") != r.get("model"))))
row("routing.requested_family != family", sum(1 for r in rd_full if r.get("requested_family") != r.get("family")))
hist = [h for r in rd_full for h in (r.get("routing_history") or [])]
row("routing.routing_history events (promotions)", f"{len(hist)} events in {sum(1 for r in rd_full if r.get('routing_history'))} runs")
row("routing.promotion from->to", dist(C(f"{h.get('from')}->{h.get('to')}" for h in hist)) if hist else "-")
row("routing.promotion reason category (route reason)", dist(C(reason_category(h.get("reason")) for h in hist)) if hist else "-")
res = [r["reserve"] for r in rd_full if isinstance(r.get("reserve"), dict)]
row("routing.reserve events", f"{len(res)}; action: {dist(C(x.get('action') for x in res)) if res else '-'}; blocked_model: {dict(C(x.get('blocked_model') for x in res))}")
row("routing.reserve.drop state/status", dist(C(f"{d.get('state')}/{d.get('status')}" for x in res for d in (x.get('drops') or []))) if res else "-")
row("routing.routing_note category", dist(C((str(r.get("routing_note")).split(":")[0] if r.get("routing_note") else None) for r in rd_full)))
row("routing.capacity.codex_family_state at dispatch", dist(C(((r.get("capacity") or {}).get("families") or {}).get("codex", {}).get("state") for r in rd_full)))
row("routing.capacity.claude_family_state at dispatch", dist(C(((r.get("capacity") or {}).get("families") or {}).get("claude", {}).get("state") for r in rd_full)))
blocked_counts = [len((r.get("scoped_limit_reasoning") or {}).get("blocked_lanes") or []) for r in rd_full if isinstance(r.get("scoped_limit_reasoning"), dict)]
elig_counts = [len((r.get("scoped_limit_reasoning") or {}).get("eligible_lanes") or []) for r in rd_full if isinstance(r.get("scoped_limit_reasoning"), dict)]
row("routing.scoped_limit_reasoning (claude runs) blocked lanes p50/max; eligible lanes p50/min",
    f"n={len(blocked_counts)}; blocked {pct(blocked_counts,50):.0f}/{max(blocked_counts)}; eligible {pct(elig_counts,50):.0f}/{min(elig_counts)}")

# launch mode
row("launch.launcher", dist(C(m.get("launcher") for m in metas)))


def launch_mode(m):
    la = m.get("launcher")
    caller = m.get("caller") if isinstance(m.get("caller"), dict) else {}
    if la == "subfleet run":
        if caller.get("waiter_pid") is not None:
            return "front door, detached + --attach inline wait"
        if m.get("adopted_at"):
            return "front door, detached (pre-created, runner adopted)"
        return "front door, detached entry never adopted by a runner"
    if la in ("subfleet claude", "subfleet codex"):
        has_rd = isinstance(m.get("routing_decision"), dict) and "class" in m["routing_decision"]
        return f"runner-created entry ({'via subfleet run sync path' if has_rd else 'direct runner call'})"
    return f"other: {la}"


row("launch.mode (derived)", dist(C(launch_mode(m) for m in metas)))
row("launch.adopted_at present", sum(1 for m in metas if m.get("adopted_at")))
row("launch.caller present (dispatched from a Claude session)", sum(1 for m in metas if isinstance(m.get("caller"), dict)))
row("launch.caller x launcher", dist(C((m.get("launcher"), isinstance(m.get("caller"), dict)) for m in metas)))
callers = [m["caller"] for m in metas if isinstance(m.get("caller"), dict)]
row("launch.caller.entrypoint", dist(C(c.get("entrypoint") for c in callers)))
row("launch.caller.mode_class", dist(C(c.get("mode_class") for c in callers)))
first_caller = min((ts(m["started_at"]) for m in metas if isinstance(m.get("caller"), dict)), default=None)
row("launch.first run with caller captured", first_caller.isoformat() if first_caller else "-")
gate_runs = [m for m in metas if re.search(r"-gate-\d{8}-\d{6}-(pr|plan)-", m.get("id", ""))]
row("runs.gate peer rounds in ledger (id has -gate-<gateid>-rN)", f"{len(gate_runs)} ({100*len(gate_runs)/N:.1f}%)")

# rc
rc = C(m.get("rc") for m in metas)
row("rc.distribution", dist(C("None(unfinished)" if k is None else str(k) for k in rc.elements())))
row("rc.grouped", f"0={rc[0]} ({100*rc[0]/N:.1f}%); quota 4={rc[4]}, 8={rc[8]}; other nonzero={sum(v for k,v in rc.items() if k not in (0,4,8,None))}; null={rc[None]}")
row("rc.by_family nonzero share", "; ".join(
    f"{fam}: {sum(1 for m in metas if m.get('family')==fam and m.get('rc') not in (0,None))}/{sum(1 for m in metas if m.get('family')==fam)}" for fam in ("claude", "codex")))
row("rc=4 by model", dict(C(m.get("model") for m in metas if m.get("rc") == 4)))
row("rc=143 by launch mode", dict(C(launch_mode(m) for m in metas if m.get("rc") == 143)))

# durations
durs = [m["duration_s"] for m in metas if isinstance(m.get("duration_s"), (int, float))]
row("duration.all finished n/p10/p50/p90/max", f"{len(durs)} / {fmt_s(pct(durs,10))} / {fmt_s(pct(durs,50))} / {fmt_s(pct(durs,90))} / {fmt_s(max(durs))}")
no_orphan = [m["duration_s"] for m in metas if not m.get("orphaned") and isinstance(m.get("duration_s"), (int, float))]
row("duration.finished, non-orphaned n/p10/p50/p90/max", f"{len(no_orphan)} / {fmt_s(pct(no_orphan,10))} / {fmt_s(pct(no_orphan,50))} / {fmt_s(pct(no_orphan,90))} / {fmt_s(max(no_orphan))}")
row("duration.orphaned runs (rc -9; duration = time until orphan sweep, not work)", sorted(round(m["duration_s"]/3600, 1) for m in metas if m.get("orphaned") and isinstance(m.get("duration_s"), (int, float))))
ok = [m["duration_s"] for m in metas if m.get("rc") == 0 and isinstance(m.get("duration_s"), (int, float))]
row("duration.rc==0 n/p10/p50/p90/max", f"{len(ok)} / {fmt_s(pct(ok,10))} / {fmt_s(pct(ok,50))} / {fmt_s(pct(ok,90))} / {fmt_s(max(ok))}")
for model in [k for k, _ in C(m.get("model") for m in metas).most_common()]:
    d = [m["duration_s"] for m in metas if m.get("model") == model and isinstance(m.get("duration_s"), (int, float))]
    if d:
        row(f"duration.{model} n/p10/p50/p90/max", f"{len(d)} / {fmt_s(pct(d,10))} / {fmt_s(pct(d,50))} / {fmt_s(pct(d,90))} / {fmt_s(max(d))}")
row("duration.total provider wall-clock hours", f"{sum(durs)/3600:.1f}")
for task in ("review", "build", "research", "adjudication", "strategy"):
    d = [m["duration_s"] for m in metas if isinstance(m.get("routing_decision"), dict) and m["routing_decision"].get("task") == task
         and isinstance(m.get("duration_s"), (int, float)) and m.get("rc") == 0]
    if d:
        row(f"duration.task={task} rc==0 n/p50/p90", f"{len(d)} / {fmt_s(pct(d,50))} / {fmt_s(pct(d,90))}")

# notify
row("notify.shape", "null | {at, pushed:bool, push:{delivered, at?, reason?, session_id?, name?, pid?, socket?, mode_class?, waiter_pid?}, surfaced:bool, surfaced_at, surfaced_by: null|'inline-waiter'|'transcript', followup?:{checked|pushes[]|lost|revive}}")
row("notify.state (replica of run_ledger._notify_state)", dist(C(notify_state(m) for m in metas)))
with_caller = [m for m in metas if isinstance(m.get("caller"), dict)]
row("notify.state among caller-dispatched runs", dist(C(notify_state(m) for m in with_caller)))
notes = [m["notify"] for m in metas if isinstance(m.get("notify"), dict)]
row("notify.pushed true/false", dist(C(bool(n.get("pushed")) for n in notes)))
row("notify.push.reason when not delivered", dist(C(EMAIL.sub("<lane>", str((n.get("push") or {}).get("reason")))[:40] for n in notes if not n.get("pushed"))))
row("notify.surfaced_by", dist(C(n.get("surfaced_by") for n in notes)))
row("notify.followup present / lost / re-pushes", f"{sum(1 for n in notes if isinstance(n.get('followup'), dict))} / {sum(1 for n in notes if (n.get('followup') or {}).get('lost'))} / {sum(len((n.get('followup') or {}).get('pushes') or []) for n in notes)}")

# per caller session fan-out
by_sess = collections.defaultdict(list)
for m in with_caller:
    by_sess[m["caller"].get("session_id")].append(m)
counts = sorted(len(v) for v in by_sess.values())
row("fanout.caller sessions (distinct caller.session_id)", len(by_sess))
row("fanout.runs per caller session min/p50/mean/p90/max", f"{counts[0]} / {pct(counts,50):.1f} / {statistics.mean(counts):.1f} / {pct(counts,90):.1f} / {counts[-1]}")
row("fanout.runs per caller session (sorted)", counts)


def intervals(ms):
    out = []
    for m in ms:
        s, e = ts(m.get("started_at")), ts(m.get("finished_at"))
        if s and e and e >= s:
            out.append((s, e))
    return out


row("fanout.max concurrent runs overall (finished runs only)", max_overlap(intervals(metas)))
row("fanout.max concurrent runs overall, excluding gate peer rounds", max_overlap(intervals([m for m in metas if m not in gate_runs])))
sess_peaks = sorted(max_overlap(intervals(v)) for v in by_sess.values())
row("fanout.max concurrent runs within one caller session", sess_peaks[-1])
row("fanout.per-session peak concurrency (sorted)", sess_peaks)
row("fanout.per-session peak concurrency p50", pct(sess_peaks, 50))
row("fanout.max runs started by one caller session within 60s / 300s",
    f"{max(max_burst([ts(m['started_at']) for m in v], 60) for v in by_sess.values())} / {max(max_burst([ts(m['started_at']) for m in v], 300) for v in by_sess.values())}")
# Orphaned runs (rc -9) carry finished_at = orphan-sweep time, 9-48h after
# start, so they inflate overlap; report the orphan-free figures as primary.
not_orphan = [m for m in metas if not m.get("orphaned")]
row("fanout.max concurrent runs overall, orphans excluded", max_overlap(intervals(not_orphan)))
row("fanout.per-session peak concurrency, orphans excluded (sorted)", sorted(max_overlap(intervals([m for m in v if not m.get("orphaned")])) for v in by_sess.values()))
# time-weighted share of concurrency levels overall (orphans excluded)
ev = []
for s, e in intervals(not_orphan):
    ev += [(s, 1), (e, -1)]
ev.sort(key=lambda x: (x[0], x[1]))
level_time = C()
cur, prev = 0, None
for t, d in ev:
    if prev is not None and cur > 0:
        level_time[cur] += (t - prev).total_seconds()
    cur += d
    prev = t
busy = sum(level_time.values())
row("fanout.time-weighted concurrency while >=1 run active", "; ".join(f"{k}:{100*v/busy:.1f}%" for k, v in sorted(level_time.items())))

# successful-only peak, dispatch bursts, mass kills
ok_runs = [m for m in metas if m.get("rc") == 0]
row("fanout.max concurrent rc==0 runs overall", max_overlap(intervals(ok_runs)))
row("fanout.per-session peak concurrency, rc==0 only (sorted)", sorted(max_overlap(intervals([m for m in v if m.get("rc") == 0])) for v in by_sess.values()))
bursts = []
for sid, ms in by_sess.items():
    xs = sorted(ms, key=lambda m: ts(m["started_at"]))
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and (ts(xs[j + 1]["started_at"]) - ts(xs[i]["started_at"])).total_seconds() <= 60:
            j += 1
        if j - i + 1 >= 3:
            grp = xs[i:j + 1]
            bursts.append((len(grp), xs[i]["started_at"][:16], sorted(C(str(m.get("rc")) for m in grp).items()),
                           len({m.get("lane") for m in grp}), sorted(C(m.get("model") for m in grp).items())))
            i = j + 1
        else:
            i += 1
bursts.sort(key=lambda b: (-b[0], b[1]))
row("fanout.dispatch bursts (>=3 runs started within 60s by one caller session)", f"n={len(bursts)}; sizes={sorted(b[0] for b in bursts)}; runs in bursts={sum(b[0] for b in bursts)} of {len(with_caller)} caller-dispatched")
for b in bursts[:6]:
    row("fanout.burst", f"size={b[0]} at={b[1]} rc={b[2]} distinct_lanes={b[3]} models={b[4]}")
kills = sorted(ts(m["finished_at"]) for m in metas if m.get("rc") == 143 and ts(m.get("finished_at")))
groups, cur_g = [], []
for t in kills:
    if cur_g and (t - cur_g[-1]).total_seconds() > 10:
        groups.append(cur_g)
        cur_g = []
    cur_g.append(t)
if cur_g:
    groups.append(cur_g)
row("rc=143 (SIGTERM) runs grouped by finishing within 10s of each other", f"{len(kills)} runs -> group sizes {sorted((len(g) for g in groups), reverse=True)}")

row("resume.resumed_from present", sum(1 for m in metas if m.get("resumed_from")))
row("resume.launcher of resumed runs", dict(C(m.get("launcher") for m in metas if m.get("resumed_from"))))
sal = [m for m in metas if m.get("salvage_refs")]
row("salvage.runs with nonempty salvage_refs", f"{len(sal)} ({100*len(sal)/N:.1f}%); total refs={sum(len(m['salvage_refs']) for m in sal)}; refs/run p50/max={pct([len(m['salvage_refs']) for m in sal],50):.0f}/{max(len(m['salvage_refs']) for m in sal)}")
row("salvage.ref namespaces", dict(C("/".join(str(r.get("ref", "")).split("/")[:2]) for m in sal for r in m["salvage_refs"])))
small = [m for m in sal if len(m["salvage_refs"]) < 1000]
row("salvage.excluding the single 13k-ref outlier: runs / refs / refs-per-run p50/max", f"{len(small)} / {sum(len(m['salvage_refs']) for m in small)} / {pct([len(m['salvage_refs']) for m in small],50):.0f}/{max(len(m['salvage_refs']) for m in small)}")
row("salvage.by rc", dict(C(m.get("rc") for m in sal)))
row("salvage.by sandbox", dict(C(((m.get("routing_decision") or {}).get("overrides") or {}).get("sandbox") for m in sal)))
row("git.head changed during run", sum(1 for m in metas if m.get("git_head_before") and m.get("git_head_after") and m["git_head_before"] != m["git_head_after"]))

# ---------------------------------------------------------------------- gates
print("\n=== GATES ===")
gates = []
round_main_responses = 0
for name in sorted(os.listdir(GATES)):
    gp = os.path.join(GATES, name, "gate.json")
    if os.path.isfile(gp):
        try:
            gates.append(json.load(open(gp)))
        except (OSError, ValueError):
            continue
        rdir = os.path.join(GATES, name, "rounds")
        if os.path.isdir(rdir):
            for sub in os.listdir(rdir):
                if os.path.isfile(os.path.join(rdir, sub, "main-response.md")):
                    round_main_responses += 1
G = len(gates)
row("gates.count", G)
created = [ts(g.get("created_at")) for g in gates if ts(g.get("created_at"))]
row("gates.created_at min/max", f"{min(created).isoformat()} .. {max(created).isoformat()}")
row("gates.state layout", "gates/<id>/{gate.json, .lock, brief.md?, certificate.json?, rounds/<attempt>/...}; gate.json keys=" + str(sorted({k for g in gates for k in g})))
row("gates.kind", dist(C(g.get("kind") for g in gates)))
row("gates.on_agreement", dist(C(g.get("on_agreement") for g in gates)))
row("gates.kind x on_agreement", dist(C((g.get("kind"), g.get("on_agreement")) for g in gates)))
row("gates.status (outcome)", dist(C(g.get("status") for g in gates)))
row("gates.status by kind", "; ".join(f"{k}: {dict(C(g.get('status') for g in gates if g.get('kind')==k))}" for k in ("pr", "plan")))
row("gates.action.status", dist(C((g.get("action") or {}).get("status") for g in gates)))
row("gates.certificate issued", sum(1 for g in gates if g.get("certificate")))
row("gates.peer", dist(C(g.get("peer") for g in gates)))
row("gates.merge_method", dist(C(g.get("merge_method") for g in gates)))
row("gates.max_rounds setting (0 = unlimited)", dist(C(g.get("max_rounds") for g in gates)))
row("gates.with round_limit_changes", sum(1 for g in gates if g.get("round_limit_changes")))
row("gates.distinct repositories (pr, count only)", len({(g.get("locator") or {}).get("repository") for g in gates if g.get("kind") == "pr"}))
row("gates.blocker category", dist(C(re.sub(r"\d+", "N", EMAIL.sub("<lane>", str(g.get("blocker"))))[:60] for g in gates if g.get("blocker"))))

nrounds = [len(g.get("rounds") or []) for g in gates]
row("gates.rounds per gate (all attempts) p50/p90/max/total", f"{pct(nrounds,50):.0f} / {pct(nrounds,90):.0f} / {max(nrounds)} / {sum(nrounds)}")
row("gates.rounds per gate histogram", dict(sorted(C(nrounds).items())))
allr = [r for g in gates for r in (g.get("rounds") or [])]
row("gates.round status", dist(C(r.get("status") for r in allr)))
verd = [len([r for r in (g.get("rounds") or []) if isinstance(r.get("verdict"), dict)]) for g in gates]
row("gates.verdict-bearing rounds per gate p50/p90/max/total", f"{pct(verd,50):.0f} / {pct(verd,90):.0f} / {max(verd)} / {sum(verd)}")
blocked_r = [len([r for r in (g.get("rounds") or []) if r.get("status") == "blocked"]) for g in gates]
row("gates.blocked (errored) rounds per gate p50/p90/max; gates with >=1", f"{pct(blocked_r,50):.0f} / {pct(blocked_r,90):.0f} / {max(blocked_r)}; {sum(1 for b in blocked_r if b)}")
top = sorted(((len(g.get('rounds') or []), dict(C(r.get('status') for r in g.get('rounds') or [])), g.get('status'), g.get('kind')) for g in gates), key=lambda x: -x[0])[:4]
row("gates.top-4 by round count (n, round statuses, gate status, kind)", top)
done = [g for g in gates if g.get("status") == "completed"]
to_approve = [len(g.get("rounds") or []) for g in done]
to_approve_v = [len([r for r in g.get("rounds") or [] if isinstance(r.get("verdict"), dict)]) for g in done]
row("gates.completed: rounds to agreement (all attempts) p50/p90/max", f"{pct(to_approve,50):.0f} / {pct(to_approve,90):.0f} / {max(to_approve)}")
row("gates.completed: verdict-bearing rounds to agreement p50/p90/max", f"{pct(to_approve_v,50):.0f} / {pct(to_approve_v,90):.0f} / {max(to_approve_v)}")
row("gates.completed: approved on first verdict", f"{sum(1 for v in to_approve_v if v == 1)} of {len(done)}")
row("gates.round peer", dist(C(r.get("peer") for r in allr)))
for peer in ("fable", "astra", "sol"):
    pr = [r for r in allr if r.get("peer") == peer]
    v = C((r.get("verdict") or {}).get("verdict") or f"no-verdict/{r.get('status')}" for r in pr)
    gs = C(g.get("status") for g in gates if g.get("peer") == peer)
    row(f"gates.peer={peer}: round verdicts | gate outcomes", f"{dist(v)} | {dist(gs)}")
row("gates.round peer_returncode", dist(C(str(r.get("peer_returncode")) for r in allr)))
row("gates.round verdict", dist(C((r.get("verdict") or {}).get("verdict") for r in allr)))
row("gates.round error category", dist(C(re.sub(r"[0-9a-f]{7,}|\d+", "N", EMAIL.sub("<lane>", str(r.get("error"))))[:70] for r in allr if r.get("error"))))
fcounts = [len((r.get("verdict") or {}).get("findings") or []) for r in allr if (r.get("verdict") or {}).get("verdict") == "changes_requested"]
row("gates.findings per changes_requested verdict p50/max", f"{pct(fcounts,50):.0f} / {max(fcounts)}")
rdur = [(ts(r.get("finished_at")) - ts(r.get("started_at"))).total_seconds() for r in allr if ts(r.get("finished_at")) and ts(r.get("started_at")) and isinstance(r.get("verdict"), dict)]
row("gates.verdict round duration n/p10/p50/p90/max", f"{len(rdur)} / {fmt_s(pct(rdur,10))} / {fmt_s(pct(rdur,50))} / {fmt_s(pct(rdur,90))} / {fmt_s(max(rdur))}")
gdur = [(ts(g.get("updated_at")) - ts(g.get("created_at"))).total_seconds() for g in done if ts(g.get("updated_at")) and ts(g.get("created_at"))]
row("gates.completed: created->updated wall clock p50/p90/max", f"{fmt_s(pct(gdur,50))} / {fmt_s(pct(gdur,90))} / {fmt_s(max(gdur))}")
row("gates.rounds carrying a main-response.md (round dir)", round_main_responses)
row("gates.rounds with peer_run_id linking to run ledger", f"{sum(1 for r in allr if r.get('peer_run_id'))} of {len(allr)}")
ledger_ids = {m.get("id") for m in metas}
row("gates.peer_run_ids still present in 500-run ledger", sum(1 for r in allr if r.get("peer_run_id") in ledger_ids))
per_gday = C(c.date().isoformat() for c in created)
row("gates.per active day n_days/p50/max", f"{len(per_gday)} / {pct(per_gday.values(),50):.0f} / {max(per_gday.values())}")

# ------------------------------------------------------------ decisions.jsonl
print("\n=== DECISIONS LOG ===")
for path in DECISIONS:
    if not os.path.isfile(path):
        row("decisions.file", f"{path.replace(os.path.expanduser('~'), '~')}: absent")
        continue
    row("decisions.file", f"{path.replace(os.path.expanduser('~'), '~')}: {os.path.getsize(path)/1e6:.0f} MB")
    n = 0
    fam, mod, task, tier, result, cls = C(), C(), C(), C(), C(), C()
    days = C()
    promos, promo_reason, reserve_n, note_cat, changed = C(), C(), 0, C(), C()
    tmin = tmax = None
    no_lane = 0
    month = C()
    storm = C()   # 2026-08-19/20: sol result=1 retry storm
    era_task = C()  # records carrying --task (task/tier routing era)
    era_task_days = C()
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            n += 1
            d = (r.get("ts") or "")[:10]
            month[d[:7]] += 1
            if d in ("2026-08-19", "2026-08-20"):
                storm[(r.get("model"), r.get("result"))] += 1
            if r.get("task"):
                era_task[(r.get("task"), r.get("tier"), r.get("model"), r.get("result"))] += 1
                era_task_days[d] += 1
            fam[r.get("family")] += 1
            mod[r.get("model")] += 1
            task[r.get("task")] += 1
            tier[r.get("tier")] += 1
            cls[r.get("class")] += 1
            result[r.get("result")] += 1
            t = r.get("ts")
            if isinstance(t, str):
                tmin = t if tmin is None or t < tmin else tmin
                tmax = t if tmax is None or t > tmax else tmax
                days[t[:10]] += 1
            for h in r.get("routing_history") or []:
                promos[f"{h.get('from')}->{h.get('to')}"] += 1
                promo_reason[reason_category(h.get("reason"))] += 1
            if isinstance(r.get("reserve"), dict):
                reserve_n += 1
            if r.get("routing_note"):
                note_cat[str(r["routing_note"]).split(":")[0]] += 1
            if r.get("requested_model") and r.get("requested_model") != r.get("model"):
                changed[f"{r.get('requested_model')}->{r.get('model')}"] += 1
            if r.get("lane/home") is None:
                no_lane += 1
    row("decisions.records (one per dispatch attempt incl. dry-runs and quota retries)", n)
    row("decisions.ts min/max", f"{tmin} .. {tmax}")
    row("decisions.per active day n_days/p50/max", f"{len(days)} / {pct(days.values(),50):.0f} / {max(days.values())}")
    row("decisions.by_family", dist(fam))
    row("decisions.by_model", dist(mod))
    row("decisions.by_task", dist(task))
    row("decisions.by_tier", dist(tier))
    row("decisions.by_class", dist(cls))
    row("decisions.result", dist(C({str(k): v for k, v in result.items()})))
    row("decisions.no lane selected", no_lane)
    row("decisions.promotion events from->to", dist(promos) if promos else "-")
    row("decisions.promotion reason category", dist(promo_reason) if promo_reason else "-")
    row("decisions.requested->actual model changed", dist(changed) if changed else "-")
    row("decisions.reserve dict present", reserve_n)
    row("decisions.routing_note category", dist(note_cat) if note_cat else "-")
    row("decisions.by_month", dict(sorted(month.items())))
    st = sum(storm.values())
    row("decisions.2026-08-19/20 storm: records; (model,result) top", f"{st}; {storm.most_common(3)}")
    row("decisions.excluding storm days", n - st)
    et = sum(era_task.values())
    row("decisions.task-routed era (--task present): records / days / first..last", f"{et} / {len(era_task_days)} / {min(era_task_days)}..{max(era_task_days)}")
    def marginal(idx):
        out = C()
        for key, v in era_task.items():
            out[str(key[idx])] += v
        return out

    row("decisions.task-routed era by task", dist(marginal(0)))
    row("decisions.task-routed era by tier", dist(marginal(1)))
    row("decisions.task-routed era by model", dist(marginal(2)))
    row("decisions.task-routed era by result", dist(marginal(3)))

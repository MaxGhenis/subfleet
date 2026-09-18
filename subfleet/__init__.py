"""subfleet — cross-account usage/quota + auth-stability monitor.

Covers the codex CODEX_HOME lanes (distinct ChatGPT accounts) and the
active Claude Code subscription. Born from the 2026-07-11 incident where an
orchestration session lost hours to invisible quota/auth state (session-limit
lane death, revoked refresh token, exhausted 5h window the app gauge hid).

Design rules:
- Server responses are ground truth; gauges and local expiry claims lie.
- Never fabricate a number: anything unreachable is "unknown" plus the last
  observed error, clearly labeled with its observation time.
- Read-only against auth stores: subfleet never writes auth.json and never
  refreshes tokens itself (an unpersisted rotation strands the lane — that is
  the revocation trap). The watchdog's expired-token heal shells out to a
  one-shot `codex exec` instead, so the codex CLI refreshes and atomically
  persists its own token.
"""

__version__ = "0.2.0"

import os as _os


def _adopt_legacy_env() -> None:
    """carpool → subfleet rename (2026-08-23): honour the old variable names for
    a while. SUBFLEET_* always wins; CARPOOL_* fills in only when unset. Scripts
    and launchd plists written before the rename keep working unchanged."""
    legacy = {"DELEGATE_CARPOOL": "DELEGATE_SUBFLEET", "CLAUDE_LANE_CARPOOL": "CLAUDE_LANE_SUBFLEET"}
    for name, value in list(_os.environ.items()):
        if name.startswith("CARPOOL_"):
            legacy[name] = "SUBFLEET_" + name[len("CARPOOL_"):]
    for old, new in legacy.items():
        if old in _os.environ and new not in _os.environ:
            _os.environ[new] = _os.environ[old]


_adopt_legacy_env()

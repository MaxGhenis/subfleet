import base64
import json

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets its own state dir so scan caches and snapshots never
    touch (or read) the real ~/chief-of-staff/state. The codex account config
    points at a nonexistent file so the repo's real codex-accounts.json
    (protected interactive account) never leaks in; tests that exercise
    protection write their own."""
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(tmp_path / "aq-state"))
    # The app home (~/.codex) is read for identity on every snapshot/pick;
    # point it at a nonexistent tmp path so tests never see the real login.
    monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(tmp_path / "app-codex-home"))
    monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(tmp_path / "codex-accounts.json"))
    monkeypatch.delenv("CLAUDE_LANE_DETACHED", raising=False)
    monkeypatch.delenv("CLAUDE_LANE_OWNED_PROMPT", raising=False)
    monkeypatch.delenv("SUBFLEET_RUN_OWNED_PROMPT", raising=False)
    # The suite often runs from inside a Claude Code session; its tool shell
    # exports the session identity that flips `subfleet run` into detached
    # mode and makes runners record a caller. Tests opt in explicitly.
    for name in (
        "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
        "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
        "SUBFLEET_RUN_DETACH", "SUBFLEET_RUN_ID", "SUBFLEET_RUN_CALLER_JSON",
        "SUBFLEET_RUN_LANE_LOG", "SUBFLEET_NOTIFY_MODE", "SUBFLEET_CLAUDE_SETTINGS",
        "SUBFLEET_CODEX_DETACHED", "SUBFLEET_REVIVE", "SUBFLEET_REVIVE_MODELS",
        "SUBFLEET_SESSION_STORE", "SUBFLEET_REVIVE_HOST", "SUBFLEET_LIVENESS_GRACE_MIN",
        "SUBFLEET_LIVENESS_MAX_AGE_H", "SUBFLEET_NOTICE_FOLLOWUP_S",
        "SUBFLEET_NOTICE_FOLLOWUP_MAX_AGE_H",
    ):
        monkeypatch.delenv(name, raising=False)
    # A finished run's notice would spawn a detached follow-up worker (a real
    # `subfleet _notice-followup` process sleeping five minutes); tests that
    # exercise the spawn turn it on and fake Popen.
    monkeypatch.setenv("SUBFLEET_NOTICE_FOLLOWUP", "off")
    # Never read the real session registry / transcripts from a test.
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(tmp_path / "dot-claude"))
    # Never touch the real tmux server, Telegram CLI, or desktop-app log/plist:
    # each points at a path that does not exist unless a test writes one.
    monkeypatch.setenv("SUBFLEET_TMUX", str(tmp_path / "no-tmux"))
    monkeypatch.delenv("SUBFLEET_TMUX_SOCKET", raising=False)
    # The desktop app's session store holds ~150k files; never read the real one.
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(tmp_path / "no-session-store"))
    monkeypatch.setenv("SUBFLEET_TG", str(tmp_path / "no-tg"))
    monkeypatch.setenv("SUBFLEET_DESKTOP_MAIN_LOG", str(tmp_path / "no-main.log"))
    monkeypatch.setenv("SUBFLEET_DESKTOP_PLIST", str(tmp_path / "no-Info.plist"))
    # The Fable reserve reads the v2 login dirs and their keychain items; tests
    # never touch the real ones. Existing dispatch tests predate the reserve
    # and exercise the picker without it; reserve tests enable it explicitly.
    monkeypatch.setenv("SUBFLEET_CLAUDE_LOGINS", str(tmp_path / "no-logins"))
    state = tmp_path / "aq-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "reserve-policy.json").write_text('{"enabled": false}')


def fake_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def make_auth_json(account_id: str, email: str = "x@example.com", plan: str = "pro") -> dict:
    access = fake_jwt(
        {
            "exp": 4102444800,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": plan,
                "chatgpt_account_id": account_id,
            },
        }
    )
    id_token = fake_jwt({"email": email})
    return {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": "2026-07-11T18:08:37.875970Z",
        "tokens": {
            "access_token": access,
            "id_token": id_token,
            "refresh_token": "rt-secret",
            "account_id": account_id,
        },
    }


@pytest.fixture
def codex_home_factory(tmp_path):
    """Create fake CODEX_HOME dirs; returns (make_home, base_path)."""
    created = []

    def make(name: str, account_id: str | None = None, email: str = "x@example.com"):
        home = tmp_path / name
        home.mkdir(parents=True, exist_ok=True)
        if account_id:
            (home / "auth.json").write_text(json.dumps(make_auth_json(account_id, email)))
        created.append(home)
        return home

    return make, tmp_path


@pytest.fixture
def env_paths(tmp_path, monkeypatch):
    """Point every subfleet path at tmp dirs."""
    state = tmp_path / "state"
    claude_dir = tmp_path / "dot-claude"
    (claude_dir / "projects").mkdir(parents=True)
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "max@example.com",
                    "organizationName": "test org",
                    "accountUuid": "acct-1",
                    "organizationUuid": "org-1",
                }
            }
        )
    )
    notify = tmp_path / "notify"
    notify_log = tmp_path / "notify.log"
    notify.write_text(
        "#!/bin/bash\n"
        f'echo "SUBJECT:$1" >> "{notify_log}"\n'
        f'echo "BODY:$2" >> "{notify_log}"\n'
    )
    notify.chmod(0o755)
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(state))
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(claude_dir))
    monkeypatch.setenv("SUBFLEET_CLAUDE_JSON", str(claude_json))
    monkeypatch.setenv("SUBFLEET_NOTIFY", str(notify))
    monkeypatch.setenv("SUBFLEET_CODEX_HOMES", "")  # no real homes by default
    return {
        "state": state,
        "claude_dir": claude_dir,
        "claude_json": claude_json,
        "notify": notify,
        "notify_log": notify_log,
        "tmp": tmp_path,
    }


def wham_ok(used=10, weekly=20, email="x@example.com", limit_reached=False, reset_at=4102444800):
    return {
        "status": "ok",
        "checked_at": "2026-07-11T12:00:00-04:00",
        "email": email,
        "plan_type": "pro",
        "allowed": not limit_reached,
        "limit_reached": limit_reached,
        "primary": {"used_percent": used, "window_seconds": 18000, "reset_at": "2026-07-11T18:29:00-04:00"},
        "secondary": {"used_percent": weekly, "window_seconds": 604800, "reset_at": "2026-07-18T12:00:00-04:00"},
        "additional": [],
    }


def wham_revoked():
    return {
        "status": "token-revoked",
        "checked_at": "2026-07-11T12:00:00-04:00",
        "error": "Encountered invalidated oauth token for user, failing request",
    }

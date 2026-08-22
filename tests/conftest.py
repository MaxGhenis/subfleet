import base64
import json

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep every test away from the operator's config, state, and accounts."""
    monkeypatch.setenv("CARPOOL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in (
        "CARPOOL_STATE_DIR",
        "CARPOOL_CLAUDE_ACCOUNTS",
        "CARPOOL_NOTIFY",
        "CARPOOL_CODEX_HOMES",
        "DELEGATE_STATE_DIR",
        "DELEGATE_ACCOUNTS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    from carpool import config

    config.save({"accounts": [], "enrolled": {}, "codex_homes": []})


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
    """Point every carpool path at tmp dirs."""
    from carpool import config

    state = config.state_dir()
    claude_dir = tmp_path / "dot-claude"
    (claude_dir / "projects").mkdir(parents=True)
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "active@example.com",
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
        "#!/bin/sh\n"
        'if [ "$#" -gt 0 ]; then payload="$*"; else payload="$(cat)"; fi\n'
        f'printf "%s\\n" "$payload" >> "{notify_log}"\n'
    )
    notify.chmod(0o755)
    monkeypatch.setenv("CARPOOL_CLAUDE_DIR", str(claude_dir))
    monkeypatch.setenv("CARPOOL_CLAUDE_JSON", str(claude_json))
    config.save(
        {
            "accounts": [],
            "enrolled": {},
            "codex_homes": [],
            "notify_cmd": str(notify),
        }
    )
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

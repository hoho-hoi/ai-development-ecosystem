"""Plan logins the broker holds on behalf of sessions.

ChatGPT plan (Codex): the broker keeps one Codex login in a CODEX_HOME of its
own (config `codex_login_dir`, created with `hb codex-login`). Its refresh
token is single use: every refresh rotates it, and a second holder refreshing
the same token gets `refresh_token_reused` and the login is lost. So the
refresh token never leaves the broker. A session gets an access-only copy of
auth.json (refresh token blanked), which works until the access token
expires (about ten days after a login or refresh), and the broker refuses to
start a session whose run could outlast it.
"""

from __future__ import annotations

import base64
import copy
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUTH_FILE = "auth.json"
# Margin on top of a profile's maxRuntimeMinutes when checking token expiry.
EXPIRY_MARGIN_SECONDS = 30 * 60


class LoginError(Exception):
    pass


def jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, without verifying it (the broker only reads the
    expiry and plan; the token is checked by OpenAI when used)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as e:
        raise LoginError(f"not a JWT: {e}") from None
    if not isinstance(data, dict):
        raise LoginError("JWT payload is not an object")
    return data


@dataclass
class ChatGptLogin:
    auth: dict[str, Any]
    expires_at: datetime
    plan: str | None
    email: str | None

    @property
    def secrets(self) -> dict[str, str]:
        """Token values to redact from the history."""
        tokens = self.auth.get("tokens") or {}
        return {f"codex:{k}": v for k, v in tokens.items() if k.endswith("token") and isinstance(v, str) and v}

    def summary(self) -> dict[str, Any]:
        return {"login": "chatgpt", "plan": self.plan, "expires_at": self.expires_at.isoformat()}

    def access_only(self) -> dict[str, Any]:
        """auth.json for a session: the refresh token is blanked (kept as an
        empty string, so the file keeps its shape)."""
        data = copy.deepcopy(self.auth)
        data["tokens"]["refresh_token"] = ""
        return data

    def check_fresh(self, seconds_needed: float, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        left = (self.expires_at - now).total_seconds()
        if left < seconds_needed + EXPIRY_MARGIN_SECONDS:
            hours = max(left, 0) / 3600
            raise LoginError(
                f"the broker's ChatGPT login expires at {self.expires_at.isoformat()} ({hours:.1f} h left), "
                f"before this session's maxRuntime ({seconds_needed / 60:.0f} min) would end. "
                "Log in again with `hb codex-login`."
            )


def load_chatgpt_login(login_dir: Path) -> ChatGptLogin:
    f = login_dir / AUTH_FILE
    if not f.is_file():
        raise LoginError(f"no Codex login in {login_dir}; run `hb codex-login` first")
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        raise LoginError(f"{f}: {e}") from None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise LoginError(f"{f} is not a ChatGPT login (no access token); run `hb codex-login`")
    claims = jwt_claims(tokens["access_token"])
    if not isinstance(claims.get("exp"), (int, float)):
        raise LoginError(f"{f}: the access token has no expiry")
    expires_at = datetime.fromtimestamp(claims["exp"], timezone.utc)
    plan = email = None
    if tokens.get("id_token"):
        try:
            idc = jwt_claims(tokens["id_token"])
            email = idc.get("email")
            plan = (idc.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
        except LoginError:
            pass
    return ChatGptLogin(auth=data, expires_at=expires_at, plan=plan, email=email)


def write_access_only(login: ChatGptLogin, codex_home: Path) -> Path:
    """Write the session's auth.json (0600) into its Codex home."""
    codex_home.mkdir(parents=True, exist_ok=True)
    target = codex_home / AUTH_FILE
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(login.access_only(), fh)
    return target

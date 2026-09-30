"""
Auth: verify Supabase JWTs on protected routes, and authenticate the
Claude-in-Chrome upload session by shared key.

Frontend sends `Authorization: Bearer <access_token>` on every API call. We
verify by asking Supabase Auth to identify the token holder — that works
regardless of which signing algorithm Supabase uses and stays correct if
Supabase rotates keys.

DIFFERS FROM THE PAY APP: roles are an ARRAY. Prompt §3 requires a person to
hold several at once (Linda is accountant and approver), so require_role
checks for an intersection rather than equality.
"""

import logging
import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .config import settings
from .supabase_client import get_anon_client, get_service_client

log = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)

ROLE_ADMIN = "admin"
ROLE_ACCOUNTANT = "accountant"
ROLE_APPROVER = "approver"
ROLE_PE = "pe"
ROLE_VIEWER = "viewer"

ALL_ROLES = (ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_APPROVER, ROLE_PE, ROLE_VIEWER)

# The label written to audit_log.actor_label when the Chrome session acts.
AGENT_ACTOR_LABEL = "chrome-agent"


class CurrentUser(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    roles: list[str] = []

    def has_any(self, *roles: str) -> bool:
        return bool(set(self.roles) & set(roles))

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    @property
    def is_accountant(self) -> bool:
        return ROLE_ACCOUNTANT in self.roles

    @property
    def is_approver(self) -> bool:
        return ROLE_APPROVER in self.roles

    @property
    def is_pe(self) -> bool:
        return ROLE_PE in self.roles


class Actor(BaseModel):
    """Who is acting on a request.

    Either a signed-in person (`user` set) or the Chrome upload session
    (`is_agent` true). Endpoints the agent may call take an Actor; endpoints
    only people may call take a CurrentUser.
    """

    user: Optional[CurrentUser] = None
    is_agent: bool = False

    @property
    def user_id(self) -> Optional[str]:
        return self.user.id if self.user else None

    @property
    def label(self) -> str:
        if self.is_agent:
            return AGENT_ACTOR_LABEL
        return self.user.email if self.user else "unknown"


# ─── User authentication ──────────────────────────────────────────────


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> CurrentUser:
    """Validate the access token and return the current user.

    401 if the token is missing, invalid, or the user has no app_users row
    (the signup trigger failed). 403 if the account is deactivated.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        anon = get_anon_client()
        user_resp = anon.auth.get_user(token)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user_resp or not user_resp.user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: no user returned",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth_user = user_resp.user
    if not auth_user.id or not auth_user.email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required claims",
        )

    # Domain restriction is enforced in Supabase Auth, but re-check here:
    # a misconfigured provider should not silently grant API access.
    if settings.allowed_email_domain:
        if not auth_user.email.lower().endswith(
            f"@{settings.allowed_email_domain.lower()}"
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires an @{settings.allowed_email_domain} account",
            )

    sb = get_service_client()
    res = (
        sb.table("app_users")
        .select("*")
        .eq("id", auth_user.id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found in app_users",
        )

    row = res.data[0]
    if row.get("deactivated_at"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account deactivated",
        )

    return CurrentUser(
        id=row["id"],
        email=row["email"],
        name=row.get("name"),
        roles=list(row.get("role") or []),
    )


def require_role(*allowed_roles: str):
    """Dependency factory for role-gating routes.

    Passes when the user holds AT LEAST ONE of the named roles.

        @router.post("/", dependencies=[Depends(require_role("admin", "accountant"))])
        def create_thing(...): ...
    """
    if not allowed_roles:
        raise ValueError("require_role needs at least one role")

    def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_any(*allowed_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(allowed_roles)}",
            )
        return user

    return _check


# ─── Agent authentication (Claude in Chrome) ──────────────────────────


def _agent_key_matches(provided: Optional[str]) -> bool:
    """Constant-time comparison of the agent key.

    Returns False when the key is unset, so a deploy that forgets
    AGENT_API_KEY denies the agent rather than accepting every caller.
    """
    if not settings.agent_api_key or not provided:
        return False
    return secrets.compare_digest(provided, settings.agent_api_key)


def require_agent_or_role(*allowed_roles: str):
    """Dependency factory for endpoints the Chrome session may call.

    Accepts either a valid `X-Agent-Key` header or a signed-in user holding
    one of the named roles. Returns an Actor so the handler can stamp
    audit_log correctly in both cases.

    Prompt §11 scopes the agent to list-approved, mark-uploaded, mark-filed,
    and flag. It is deliberately NOT accepted on approve — prompt §12 requires
    a human stamp before anything reaches BuilderTrend.
    """
    if not allowed_roles:
        raise ValueError("require_agent_or_role needs at least one role")

    def _check(
        x_agent_key: Optional[str] = Header(default=None, alias="X-Agent-Key"),
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    ) -> Actor:
        if _agent_key_matches(x_agent_key):
            return Actor(is_agent=True)

        # A wrong (not merely absent) agent key is a distinct failure worth
        # logging — it means a real session is misconfigured or someone is
        # probing. Fall through to user auth either way.
        if x_agent_key:
            log.warning("Rejected X-Agent-Key: value did not match")

        user = get_current_user(credentials)
        if not user.has_any(*allowed_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(allowed_roles)}",
            )
        return Actor(user=user)

    return _check


def require_agent_only():
    """Dependency for endpoints ONLY the Chrome session may call."""

    def _check(
        x_agent_key: Optional[str] = Header(default=None, alias="X-Agent-Key"),
    ) -> Actor:
        if not _agent_key_matches(x_agent_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Valid X-Agent-Key required",
            )
        return Actor(is_agent=True)

    return _check


def require_job_key():
    """Dependency for the scheduled job endpoints (prompt §11).

    Render cron authenticates with the same shared key as the agent. These
    endpoints trigger polling and email sends, so they must never be open.
    """

    def _check(
        x_agent_key: Optional[str] = Header(default=None, alias="X-Agent-Key"),
    ) -> Actor:
        if not _agent_key_matches(x_agent_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Valid X-Agent-Key required",
            )
        return Actor(is_agent=True)

    return _check


def as_actor(user: CurrentUser = Depends(get_current_user)) -> Actor:
    """Wrap an authenticated user as an Actor, for handlers that log both."""
    return Actor(user=user)

"""
Current user API. The frontend calls this after login to learn its roles —
the Supabase session carries only email and name, so the backend is the
authority on what the user may do (prompt §2).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from ..core.auth import CurrentUser, get_current_user
from ..core.supabase_client import get_service_client

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=CurrentUser)
def get_me(user: CurrentUser = Depends(get_current_user)):
    # Best-effort last-login stamp. A failure here must not block the call
    # that the whole frontend waits on before it can render anything.
    try:
        get_service_client().table("app_users").update(
            {"last_login_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", user.id).execute()
    except Exception:
        pass
    return user

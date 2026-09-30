"""
FastAPI app entry point.

Run locally:
    uvicorn app.main:app --reload --port 8000

In production (Render):
    uvicorn app.main:app --host 0.0.0.0 --port $PORT

Structured JSON logging with the invoice id as correlation id (prompt §12)
arrives with the invoice endpoints in Phase 1; for now every 500 carries an
error_id that also appears in the Render logs.
"""

import logging
import uuid

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import admin, invoices, jobs, me, projects
from .core.config import settings

logging.basicConfig(level=settings.log_level)
log = logging.getLogger("ferrocrete-invoices")


app = FastAPI(
    title=settings.app_name,
    description=(
        "Vendor invoice intake, AI cost-code suggestion, review and approval "
        "for Ferrocrete Builders. Approved invoices are pushed to "
        "BuilderTrend by a Claude in Chrome session — never by this API."
    ),
    version="0.1.0",
)


# ─── CORS ─────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Expose X-Error-Id so the browser client can read it off a 500. Without
    # this Chrome treats some error responses as opaque and the JS client
    # reports "Failed to fetch" instead of the real status.
    expose_headers=["X-Error-Id"],
)


# ─── Health check ─────────────────────────────────────────────────────
@app.get("/health", tags=["meta"])
def health():
    """Liveness plus a view of which integrations are actually wired up.

    Deploys fail quietly when a key is missing, so the health check reports
    what is configured rather than just "ok".
    """
    return {
        "status": "ok",
        "env": settings.app_env,
        "name": settings.app_name,
        "integrations": {
            "claude": settings.claude_enabled,
            "drive": settings.drive_enabled,
            "email": settings.email_enabled,
            "agent_auth": settings.agent_auth_enabled,
        },
        "jobs_paused": settings.jobs_paused,
    }


# ─── Error handlers ───────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Cleaner 422s than FastAPI's default.

    Write schemas use extra="forbid", so a smuggled field (a PATCH trying to
    set `status` or `onboarded_at`) lands here as a named rejection rather
    than being silently dropped.
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation error", "errors": exc.errors()},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code >= 500:
        log.error(
            "HTTPException %d in %s %s: %s",
            exc.status_code,
            request.method,
            request.url.path,
            exc.detail,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers or None,
    )


@app.exception_handler(Exception)
async def fallback_handler(request: Request, exc: Exception):
    """Catch-all: never leak a stack trace in production, always log one."""
    error_id = uuid.uuid4().hex[:12]
    log.exception(
        "Unhandled %s in %s %s [error_id=%s]",
        type(exc).__name__,
        request.method,
        request.url.path,
        error_id,
    )
    body = {
        "detail": (
            f"{type(exc).__name__}: {exc}"
            if settings.is_dev
            else f"Server error (id: {error_id}). Check Render logs for [error_id={error_id}]."
        ),
        "error_id": error_id,
        "type": type(exc).__name__,
    }
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=body,
        headers={"X-Error-Id": error_id},
    )


# ─── Mount routes ─────────────────────────────────────────────────────
app.include_router(me.router)
app.include_router(projects.router)
app.include_router(invoices.router)
app.include_router(jobs.router)
app.include_router(admin.router)

# Still to come, and deliberately absent rather than stubbed — a route that
# returns 501 still looks live to the Chrome session, and nothing should look
# live until it enforces the §12 guardrails:
#   Phase 2: assign, reassign, mark-reviewed, approve, reject, and the two
#            scheduled email jobs.
#   Phase 3: the agent-authenticated mark-uploaded and mark-filed, and filing.

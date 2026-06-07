from __future__ import annotations

from fastapi import Cookie, Header, HTTPException, status

from app.config import settings


def admin_auth_required() -> bool:
    return bool(settings.admin_api_token) or settings.app_env.strip().lower() != "local"


def require_admin_api_token(
    x_admin_token: str | None = Header(default=None),
    admin_api_token: str | None = Cookie(default=None),
) -> None:
    expected = settings.admin_api_token
    if not expected:
        if settings.app_env.strip().lower() == "local":
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_API_TOKEN must be configured outside local app_env",
        )

    supplied = x_admin_token or admin_api_token
    if supplied != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing admin token")

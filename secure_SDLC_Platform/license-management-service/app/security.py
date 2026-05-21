import hmac

from fastapi import Header, HTTPException, status

from .settings import get_settings


def require_admin(x_horizon_admin_key: str = Header(default="")) -> str:
    expected = get_settings().admin_api_key
    if not expected or not hmac.compare_digest(x_horizon_admin_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Horizon admin API key.",
        )
    return "admin"


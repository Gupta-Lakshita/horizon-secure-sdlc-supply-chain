from fastapi import Header, HTTPException


def get_client_id(x_client_id: str = Header(...)) -> str:
    """Client ID always from header (set by runner from config.client_id). Never from body."""
    if not x_client_id:
        raise HTTPException(status_code=401, detail="X-Client-Id header required")
    return x_client_id

from fastapi import Header, HTTPException


def get_current_user_id(authorization: str | None = Header(default=None, alias="Authorization")) -> str:
    # Placeholder Firebase hook. In production, verify ID token with firebase-admin.
    # For local development, pass: "Authorization: Bearer <user_uuid>"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return token


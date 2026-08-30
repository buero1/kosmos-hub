from secrets import compare_digest, token_urlsafe

from fastapi import HTTPException, Request


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str):
        token = token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def require_csrf(request: Request, provided_token: str) -> None:
    expected_token = request.session.get("csrf_token")
    if not isinstance(expected_token, str) or not compare_digest(expected_token, provided_token):
        raise HTTPException(status_code=403, detail="Invalid form token.")

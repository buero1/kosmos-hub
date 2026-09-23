"""Progressive form submissions: report failed redirects without leaving the editor."""
import json
from urllib.parse import parse_qs, urlsplit

from starlette.datastructures import Headers


FORM_HEADER = "x-hub-form"
ERROR_STATES = {"error", "failed", "failure", "not-approved"}
STATE_KEYS = {
    "fields", "layout", "state", "status", "activity", "calendar", "communication",
    "note", "email", "email_state", "sync", "access", "case_link", "email_link",
    "mailbox", "zoho", "zoho_books", "backup", "update",
    "linked", "fresh_users", "fresh_backups", "fresh_updates", "plugin_install",
    "direct_update", "complete_update", "refresh", "backup_refresh", "backup_action",
    "users", "maintenance",
}


def redirect_error(location: str) -> str | None:
    params = parse_qs(urlsplit(location).query)
    for key, values in params.items():
        if key not in STATE_KEYS and f"{key}_message" not in params:
            continue
        if any(value in ERROR_STATES or value.endswith("-failed") for value in values):
            messages = params.get(f"{key.removesuffix('_state')}_message") or params.get("message")
            return messages[0] if messages else "Die Aktion konnte nicht abgeschlossen werden. Bitte die Eingaben pruefen."
    return None


class FormResponseMiddleware:
    """Keep legacy native responses; opt-in fetch forms receive redirect outcomes.

    Intercept only ASGI messages, so endpoint background tasks and session cookies
    still run unchanged. Never consume or retain submitted fields/files here.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or Headers(scope=scope).get(FORM_HEADER) != "preserve":
            return await self.app(scope, receive, send)
        replacement = None

        async def form_send(message):
            nonlocal replacement
            if message["type"] == "http.response.start" and message["status"] in {301, 302, 303, 307, 308}:
                location = Headers(raw=message["headers"]).get("location", "")
                error = redirect_error(location)
                if message["status"] in {307, 308}:
                    # Do not replay a POST automatically or silently change it to GET.
                    error = "Die Formularadresse hat sich geaendert. Die Eingaben bleiben erhalten; bitte die Adresse pruefen."
                payload = {"detail": error} if error else {"redirect_url": location}
                replacement = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                headers = [(key, value) for key, value in message["headers"] if key.lower() not in {
                    b"location", b"content-type", b"content-length", b"cache-control",
                }]
                headers.extend([(b"content-type", b"application/json"),
                                (b"content-length", str(len(replacement)).encode()), (b"cache-control", b"no-store")])
                message = {**message, "status": 400 if error else 200, "headers": headers}
            elif message["type"] == "http.response.body" and replacement is not None:
                if message.get("more_body", False):
                    return
                message = {"type": "http.response.body", "body": replacement}
            await send(message)

        await self.app(scope, receive, form_send)

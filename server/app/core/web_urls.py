"""Web-only links for CRM field display; never fetch the target URL."""

import re
from urllib.parse import urlsplit


def web_url_href(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value or "\\" in value or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    has_scheme = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value))
    href = "https:" + value if value.startswith("//") else value if has_scheme else "https://" + value
    try:
        parsed = urlsplit(href)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        if not has_scheme and "." not in parsed.hostname:
            return None
        _ = parsed.port
    except ValueError:
        return None
    return href

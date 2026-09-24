"""Build an external search link without making any external requests."""

from urllib.parse import urlencode


def business_google_search_url(*parts: str | None) -> str:
    query = " ".join(" ".join(part.split()) for part in parts if part and part.strip())
    return "https://www.google.com/search?" + urlencode({"q": query})

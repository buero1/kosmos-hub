"""Rich offer text shared by forms, operations and PDF rendering."""

from html import unescape
import re

from app.services.customer_communications import _EmailComposerHtmlSanitizer


OFFER_NOTES_TOKEN = "${Offer.Notes}"
OFFER_NOTES_MAX_LENGTH = 20_000


def sanitize_offer_notes(value: str) -> str:
    if len(value) > OFFER_NOTES_MAX_LENGTH:
        raise ValueError("Anmerkungen sind zu lang (maximal 20.000 Zeichen).")
    sanitizer = _EmailComposerHtmlSanitizer(allow_template_href_placeholders=True)
    sanitizer.feed(value)
    sanitizer.close()
    result = sanitizer.content().strip()
    # An emptied rich-text editor often submits <p><br></p>, not an empty string.
    if not unescape(re.sub(r"<[^>]+>", "", result)).strip() and "<img " not in result:
        return ""
    return result

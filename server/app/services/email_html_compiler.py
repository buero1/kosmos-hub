"""Local compatibility compiler for Hub-authored outgoing email."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape, unescape
from html.parser import HTMLParser
import os
import re

from app.services.email_composer_settings import (
    DEFAULT_FONT_FAMILY,
    DEFAULT_FONT_SIZE,
    DEFAULT_LINE_HEIGHT,
)

EMAIL_HTML_COMPILER_VERSION = "2026.09.08.5"
_COMPILER_BYPASS_ENVIRONMENT_VARIABLE = "KOSMOS_EMAIL_HTML_COMPILER_BYPASS"

_STYLE_TAG_PATTERN = re.compile(r"<style\b[^>]*>(?P<content>.*?)</style\s*>", flags=re.IGNORECASE | re.DOTALL)
_CSS_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", flags=re.DOTALL)
_SAFE_CSS_PROPERTY_NAMES = frozenset({
    "background", "background-color", "border", "border-bottom", "border-color", "border-radius", "border-spacing",
    "border-style", "border-width", "box-sizing", "color", "display", "font-family", "font-size", "font-style",
    "font-weight", "height", "letter-spacing", "line-height", "margin", "margin-bottom", "margin-left", "margin-right",
    "margin-top", "max-width", "min-width", "mso-line-height-rule", "padding", "padding-bottom", "padding-left",
    "padding-right", "padding-top", "text-align", "text-decoration", "vertical-align", "width",
})
_SAFE_CSS_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9#%(),.\s/+_!'-]+$")
_SIMPLE_SELECTOR_PATTERN = re.compile(
    r"(?P<tag>[a-z][a-z0-9-]*)?(?P<identifiers>(?:[.#][A-Za-z][A-Za-z0-9_-]{0,63})*)$",
    flags=re.IGNORECASE,
)
_CSS_BUTTON_CLASS_PATTERN = re.compile(r"(?:^|[-_\s])(?:button|btn|cta)(?:$|[-_\s])", flags=re.IGNORECASE)
_HEX_COLOR_PATTERN = re.compile(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?(?![0-9a-f])", flags=re.IGNORECASE)
_RGB_COLOR_PATTERN = re.compile(
    r"rgba?\(\s*(?P<red>\d{1,3})\s*,\s*(?P<green>\d{1,3})\s*,\s*(?P<blue>\d{1,3})(?:\s*,\s*[\d.]+)?\s*\)",
    flags=re.IGNORECASE,
)
_FIXED_WIDTH_PATTERN = re.compile(r"\b(?:width\s*=\s*[\"']?)(?P<width>\d{3,4})", flags=re.IGNORECASE)
_FIXED_LAYOUT_WIDTH_VALUE_PATTERN = re.compile(r"^(?P<width>\d{3,4})(?:px)?$", flags=re.IGNORECASE)
_NUMERIC_LINE_HEIGHT_PATTERN = re.compile(r"^\d+(?:\.\d+)?$")
_LONG_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]{80,}", flags=re.IGNORECASE)
_IMG_TAG_PATTERN = re.compile(r"<img\b(?P<attributes>[^>]*)>", flags=re.IGNORECASE | re.DOTALL)
_IMAGE_WIDTH_ATTRIBUTE_PATTERN = re.compile(r"\bwidth\s*=\s*[\"'][^\"']+[\"']", flags=re.IGNORECASE)
_IMAGE_WIDTH_STYLE_PATTERN = re.compile(r"\bwidth\s*:\s*[^;\"']+", flags=re.IGNORECASE)
_VOID_HTML_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_FLUID_LAYOUT_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "div", "figcaption", "figure", "footer", "header",
    "h1", "h2", "h3", "h4", "h5", "h6", "main", "nav", "ol", "p", "pre", "section", "span", "ul",
})
_FIXED_LAYOUT_WIDTH_MIN_PX = 320
_FIXED_LAYOUT_WIDTH_MAX_PX = 1600
_GMAIL_CLIP_WARNING_THRESHOLD_BYTES = 102 * 1024


def _email_line_height(value: str | float) -> str:
    """Use a CSS value that Outlook Classic renders consistently."""
    normalized_value = str(value).strip()
    if _NUMERIC_LINE_HEIGHT_PATTERN.fullmatch(normalized_value):
        return f"{float(normalized_value) * 100:g}%"
    return normalized_value


@dataclass(frozen=True)
class EmailHtmlCompilation:
    """The authored source, exact SMTP HTML and non-blocking preflight notes."""

    source_html: str
    compiled_html: str
    version: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _CssRule:
    selectors: tuple[str, ...]
    declarations: tuple[tuple[str, str], ...]
    is_mobile: bool


@dataclass
class _ButtonContext:
    attrs: list[tuple[str, str | None]]
    background_color: str
    text_color: str
    parts: list[str]


class _EmailMarkupValidator(HTMLParser):
    """Report malformed source without changing what the sender authored."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._open_tags: list[str] = []
        self._warnings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag not in _VOID_HTML_TAGS:
            self._open_tags.append(normalized_tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in _VOID_HTML_TAGS:
            self._warnings.append(f"Das leere HTML-Element <{normalized_tag}> darf nicht geschlossen werden.")
            return
        if not self._open_tags:
            self._warnings.append(f"Das schließende HTML-Element </{normalized_tag}> hat kein passendes öffnendes Element.")
            return
        if self._open_tags[-1] == normalized_tag:
            self._open_tags.pop()
            return
        if normalized_tag in self._open_tags:
            expected_tag = self._open_tags[-1]
            self._warnings.append(
                f"HTML ist nicht korrekt verschachtelt: Vor </{normalized_tag}> wird </{expected_tag}> erwartet."
            )
            while self._open_tags and self._open_tags[-1] != normalized_tag:
                self._open_tags.pop()
            self._open_tags.pop()
            return
        self._warnings.append(f"Das schließende HTML-Element </{normalized_tag}> hat kein passendes öffnendes Element.")

    def warnings(self) -> tuple[str, ...]:
        if self._open_tags:
            unclosed_tags = ", ".join(f"<{tag}>" for tag in dict.fromkeys(self._open_tags))
            self._warnings.append(f"HTML enthält nicht geschlossene Elemente: {unclosed_tags}.")
            self._open_tags.clear()
        return tuple(dict.fromkeys(self._warnings))

    @classmethod
    def warnings_for(cls, source: str) -> tuple[str, ...]:
        validator = cls()
        validator.feed(source)
        validator.close()
        return validator.warnings()


class _EmailMarkupTransformer(HTMLParser):
    """Inline safe CSS and add email-client-safe structural markup."""

    def __init__(
        self,
        rules: tuple[_CssRule, ...],
        *,
        line_height: float,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self.rules = rules
        self.line_height = line_height
        self.parts: list[str] = []
        self._buttons: list[_ButtonContext] = []
        self._open_tags: list[str] = []
        self._outlook_table_wrappers: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "a" and self._buttons:
            button = self._buttons.pop()
            self._append(self._bulletproof_button(button))
            self._close_tag(normalized_tag)
            return
        if normalized_tag == "table":
            self._append("</table>")
            self._close_tag(normalized_tag)
            if self._outlook_table_wrappers and self._outlook_table_wrappers.pop():
                self._append("<!--[if mso]></td></tr></table><![endif]-->")
            return
        self._append(f"</{normalized_tag}>")
        self._close_tag(normalized_tag)

    def handle_data(self, data: str) -> None:
        self._append(data)

    def handle_entityref(self, name: str) -> None:
        self._append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._append(f"<!--{data}-->")

    def content(self) -> str:
        while self._buttons:
            self._append(self._bulletproof_button(self._buttons.pop()))
        return "".join(self.parts)

    def _start_tag(self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool) -> None:
        normalized_tag = tag.casefold()
        rendered_attrs, styles, classes, outlook_table_width = self._styled_attributes(
            normalized_tag,
            attrs,
            in_table_cell="td" in self._open_tags and "p" not in self._open_tags,
        )
        if normalized_tag == "a" and not closed and self._is_button(styles, classes):
            self._buttons.append(
                _ButtonContext(
                    attrs=rendered_attrs,
                    background_color=self._background_color(styles),
                    text_color=self._outlook_color(styles.get("color", ""), fallback="#ffffff"),
                    parts=[],
                )
            )
            self._open_tags.append(normalized_tag)
            return
        if normalized_tag == "table" and outlook_table_width is not None and not closed:
            self._append(self._outlook_table_open(outlook_table_width))
        self._append(self._render_tag(normalized_tag, rendered_attrs, closed=closed))
        if not closed and normalized_tag not in _VOID_HTML_TAGS:
            self._open_tags.append(normalized_tag)
            if normalized_tag == "table":
                self._outlook_table_wrappers.append(outlook_table_width is not None)

    def _styled_attributes(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        in_table_cell: bool,
    ) -> tuple[list[tuple[str, str | None]], dict[str, str], set[str], int | None]:
        values = {name.casefold(): value or "" for name, value in attrs}
        classes = {item for item in values.get("class", "").split() if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", item)}
        element_id = values.get("id", "")
        styles: dict[str, str] = {}
        for rule in self.rules:
            if any(self._selector_matches(selector, tag, classes, element_id) for selector in rule.selectors):
                styles.update(rule.declarations)
        styles.update(self._safe_declarations(values.get("style", "")))
        outlook_table_width: int | None = None

        if tag == "table":
            outlook_table_width = self._fixed_layout_width(values, styles)
            if outlook_table_width is not None:
                # Modern clients use the fluid table; classic Outlook gets the original width in a conditional wrapper.
                classes.add("hub-email-fluid-container")
                values["width"] = "100%"
                styles["width"] = "100%"
                styles.setdefault("max-width", f"{outlook_table_width}px")
                styles["min-width"] = "0"
            styles.update({
                "border-collapse": "collapse",
                "mso-table-lspace": "0pt",
                "mso-table-rspace": "0pt",
            })
            values.setdefault("role", "presentation")
            values.setdefault("cellpadding", "0")
            values.setdefault("cellspacing", "0")
            values.setdefault("border", "0")
        elif tag in {"td", "th"}:
            styles.setdefault("border-collapse", "collapse")

        if tag in {"table", "td", "th"}:
            # bgcolor remains a dependable background-color fallback in legacy mail clients.
            background_color = self._outlook_color(
                styles.get("background-color", "") or styles.get("background", ""),
                fallback="",
            )
            if background_color:
                values.setdefault("bgcolor", background_color)
        elif tag == "p":
            # Preserve a stable layout for paragraphs from existing templates.
            styles.setdefault("margin", "0 0 10px")
            styles.setdefault("line-height", _email_line_height(self.line_height))
            styles.setdefault("mso-line-height-rule", "exactly")
        elif tag == "img":
            styles.setdefault("max-width", "100%")
            styles.setdefault("height", "auto")
            styles.setdefault("border", "0")
            styles.setdefault("outline", "none")
            styles.setdefault("text-decoration", "none")
            if in_table_cell and (values.get("width", "").strip() or "width" in styles):
                styles.setdefault("display", "block")

        if tag in _FLUID_LAYOUT_TAGS:
            fixed_layout_width = self._fixed_layout_width({}, styles)
            if fixed_layout_width is not None:
                classes.add("hub-email-fluid-element")
                styles["width"] = "100%"
                styles.setdefault("max-width", f"{fixed_layout_width}px")
                styles["min-width"] = "0"

        if "line-height" in styles:
            styles["line-height"] = _email_line_height(styles["line-height"])
            if tag == "p":
                styles.setdefault("mso-line-height-rule", "exactly")

        rendered: list[tuple[str, str | None]] = []
        emitted = set()
        for name, value in attrs:
            normalized_name = name.casefold()
            if normalized_name in {"class", "style"}:
                continue
            rendered.append((normalized_name, values["width"] if normalized_name == "width" and tag == "table" and outlook_table_width is not None else value))
            emitted.add(normalized_name)
        for name in ("role", "cellpadding", "cellspacing", "border", "bgcolor"):
            if name in values and name not in emitted:
                rendered.append((name, values[name]))
        if classes:
            rendered.append(("class", " ".join(sorted(classes))))
        if styles:
            rendered.append(("style", "; ".join(f"{name}: {value}" for name, value in styles.items())))
        return rendered, styles, classes, outlook_table_width

    @staticmethod
    def _fixed_layout_width(values: dict[str, str], styles: dict[str, str]) -> int | None:
        """Return a desktop content width, never a relative or deliberately small column width."""
        for candidate in (values.get("width", ""), styles.get("width", ""), styles.get("min-width", "")):
            match = _FIXED_LAYOUT_WIDTH_VALUE_PATTERN.fullmatch(candidate.strip())
            if match is None:
                continue
            width = int(match.group("width"))
            if _FIXED_LAYOUT_WIDTH_MIN_PX <= width <= _FIXED_LAYOUT_WIDTH_MAX_PX:
                return width
        return None

    @staticmethod
    def _outlook_table_open(width: int) -> str:
        return (
            "<!--[if mso]>"
            f'<table role="presentation" width="{width}" cellpadding="0" cellspacing="0" border="0" '
            f'style="width: {width}px; border-collapse: collapse; mso-table-lspace: 0pt; mso-table-rspace: 0pt;"><tr><td>'
            "<![endif]-->"
        )

    @staticmethod
    def _safe_declarations(style: str) -> dict[str, str]:
        declarations: dict[str, str] = {}
        for declaration in style.split(";"):
            name, separator, value = declaration.partition(":")
            normalized_name = name.strip().casefold()
            normalized_value = value.strip()
            if normalized_value.casefold().endswith("!important"):
                normalized_value = normalized_value[:-10].rstrip()
            if (
                separator
                and normalized_name in _SAFE_CSS_PROPERTY_NAMES
                and normalized_value
                and "url(" not in normalized_value.casefold()
                and _SAFE_CSS_VALUE_PATTERN.fullmatch(normalized_value)
            ):
                declarations[normalized_name] = normalized_value
        return declarations

    @staticmethod
    def _selector_matches(selector: str, tag: str, classes: set[str], element_id: str) -> bool:
        match = _SIMPLE_SELECTOR_PATTERN.fullmatch(selector)
        if match is None:
            return False
        selector_tag = (match.group("tag") or "").casefold()
        if selector_tag and selector_tag != tag:
            return False
        for identifier in re.findall(r"[.#][A-Za-z][A-Za-z0-9_-]{0,63}", match.group("identifiers")):
            if identifier.startswith(".") and identifier[1:] not in classes:
                return False
            if identifier.startswith("#") and identifier[1:] != element_id:
                return False
        return True

    @staticmethod
    def _render_tag(tag: str, attrs: list[tuple[str, str | None]], *, closed: bool) -> str:
        rendered = "".join(
            f" {name}" if value is None else f' {name}="{escape(value, quote=True)}"'
            for name, value in attrs
        )
        return f"<{tag}{rendered}{' /' if closed else ''}>"

    @staticmethod
    def _is_button(styles: dict[str, str], classes: set[str]) -> bool:
        return bool(
            _CSS_BUTTON_CLASS_PATTERN.search(" ".join(classes))
            or ("background" in styles or "background-color" in styles) and "padding" in styles
        )

    @classmethod
    def _background_color(cls, styles: dict[str, str]) -> str:
        return cls._outlook_color(
            styles.get("background-color", "") or styles.get("background", ""),
            fallback="#297db8",
        )

    @staticmethod
    def _outlook_color(value: str, *, fallback: str) -> str:
        """Normalize editor colors to the hexadecimal notation used by Outlook VML."""
        hex_match = _HEX_COLOR_PATTERN.search(value)
        if hex_match:
            color = hex_match.group(0).lower()
            if len(color) == 4:
                return f"#{color[1] * 2}{color[2] * 2}{color[3] * 2}"
            return color

        rgb_match = _RGB_COLOR_PATTERN.search(value)
        if rgb_match:
            channels = tuple(int(rgb_match.group(name)) for name in ("red", "green", "blue"))
            if all(channel <= 255 for channel in channels):
                return "#{:02x}{:02x}{:02x}".format(*channels)
        return fallback

    @classmethod
    def _bulletproof_button(cls, button: _ButtonContext) -> str:
        attributes = {name: value or "" for name, value in button.attrs}
        href = attributes.get("href", "")
        label = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", "".join(button.parts)))).strip() or "Öffnen"
        width = min(320, max(132, len(label) * 8 + 44))
        anchor = cls._render_tag("a", button.attrs, closed=False) + "".join(button.parts) + "</a>"
        return (
            "<!--[if mso]>"
            f'<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="{escape(href, quote=True)}" '
            f'style="height:42px;v-text-anchor:middle;width:{width}px;" arcsize="10%" stroke="f" fillcolor="{button.background_color}">'
            "<w:anchorlock/><center "
            f'style="color:{button.text_color};font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:bold;">{escape(label)}</center>'
            "</v:roundrect><![endif]-->"
            "<!--[if !mso]><!-- -->"
            f"{anchor}"
            "<!--<![endif]-->"
        )

    def _append(self, value: str) -> None:
        if self._buttons:
            self._buttons[-1].parts.append(value)
        else:
            self.parts.append(value)

    def _close_tag(self, tag: str) -> None:
        for index in range(len(self._open_tags) - 1, -1, -1):
            if self._open_tags[index] == tag:
                del self._open_tags[index:]
                return


class EmailHtmlCompiler:
    """Compile Hub-authored rich text into a portable, locally produced email document.

    The input has already passed the Hub's email sanitizer. This compiler adds
    client-compatibility and layout structure without network calls or a
    third-party service. Legacy Zoho HTML is intentionally excluded by callers.
    """

    _BODY_OPEN_PATTERN = re.compile(r"<body\b[^>]*>", flags=re.IGNORECASE)
    _BODY_CLOSE_PATTERN = re.compile(r"</body\s*>", flags=re.IGNORECASE)
    _HEAD_PATTERN = re.compile(r"<head\b[^>]*>.*?</head\s*>", flags=re.IGNORECASE | re.DOTALL)
    _DOCUMENT_TAG_PATTERN = re.compile(r"</?(?:!doctype|html)\b[^>]*>", flags=re.IGNORECASE)

    def compile(
        self,
        source_html: str,
        *,
        source_stylesheet: str = "",
        font_family: str = DEFAULT_FONT_FAMILY,
        font_size: int = DEFAULT_FONT_SIZE,
        line_height: float = DEFAULT_LINE_HEIGHT,
    ) -> EmailHtmlCompilation:
        source = source_html.strip()
        if os.getenv(_COMPILER_BYPASS_ENVIRONMENT_VARIABLE, "").strip() == "1":
            return EmailHtmlCompilation(
                source_html=source,
                compiled_html=source,
                version=f"{EMAIL_HTML_COMPILER_VERSION}-bypassed",
                warnings=("Der Hub-E-Mail-Compiler wurde für diesen Versand temporär umgangen.",),
            )
        stylesheet = "\n".join(filter(None, (source_stylesheet, self.extract_stylesheet(source))))
        desktop_rules, mobile_rules, stylesheet_warnings = self._parse_stylesheet(stylesheet)
        fragment = self._transform_fragment(
            self._body_fragment(source),
            desktop_rules,
            line_height=line_height,
        )
        warnings = self._preflight_warnings(source, stylesheet, stylesheet_warnings)
        mobile_styles = self._render_mobile_styles(mobile_rules)
        outlook_list_styles = self._outlook_list_styles(source)
        compiled = f"""<!doctype html>
<html lang="de" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">
<head>
  <meta charset="utf-8">
  <meta http-equiv="x-ua-compatible" content="ie=edge">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="x-apple-disable-message-reformatting">
  <title></title>
  <!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
  <style>
    html, body {{ margin: 0 !important; padding: 0 !important; width: 100% !important; min-width: 100% !important; }}
    table {{ border-spacing: 0; border-collapse: collapse; mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
    td {{ border-collapse: collapse; }}
    img {{ -ms-interpolation-mode: bicubic; }}
    a {{ text-decoration: none; }}
    a[x-apple-data-detectors] {{ color: inherit !important; font: inherit !important; line-height: inherit !important; text-decoration: none !important; }}
    @media screen and (max-width: 600px) {{
      .hub-email-mobile-stack {{ box-sizing: border-box !important; display: block !important; width: 100% !important; }}
{mobile_styles}    }}
  </style>
{outlook_list_styles}</head>
<body style="margin: 0; padding: 0; width: 100%; background-color: #ffffff;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="width: 100%; margin: 0; padding: 0; background-color: #ffffff;">
    <tr>
      <td style="padding: 0; color: #111827; font-family: {escape(font_family, quote=True)}; font-size: {font_size}px; line-height: {_email_line_height(line_height)}; mso-line-height-rule: exactly; text-align: left;">
{fragment}
      </td>
    </tr>
  </table>
</body>
</html>"""
        return EmailHtmlCompilation(
            source_html=source,
            compiled_html=compiled,
            version=EMAIL_HTML_COMPILER_VERSION,
            warnings=tuple(dict.fromkeys((*warnings, *self._compiled_warnings(compiled)))),
        )

    @staticmethod
    def extract_stylesheet(source_html: str) -> str:
        """Read stylesheet blocks; HTML itself remains subject to the existing sanitizer."""
        return "\n".join(match.group("content") for match in _STYLE_TAG_PATTERN.finditer(source_html))

    @classmethod
    def sanitize_stylesheet(cls, source_html: str) -> str:
        """Persist only the CSS subset the local compiler can later reproduce."""
        desktop_rules, mobile_rules, _warnings = cls._parse_stylesheet(cls.extract_stylesheet(source_html))
        desktop = cls._render_stylesheet_rules(desktop_rules)
        mobile = cls._render_stylesheet_rules(mobile_rules)
        if not mobile:
            return desktop
        return f"{desktop}\n@media screen and (max-width: 600px) {{\n{mobile}\n}}".strip()

    @classmethod
    def _body_fragment(cls, source_html: str) -> str:
        """Keep the body of a pasted document, otherwise retain its fragment."""
        body_open = cls._BODY_OPEN_PATTERN.search(source_html)
        if body_open:
            body_close = cls._BODY_CLOSE_PATTERN.search(source_html, body_open.end())
            fragment = source_html[body_open.end():body_close.start() if body_close else None]
        else:
            fragment = cls._DOCUMENT_TAG_PATTERN.sub("", cls._HEAD_PATTERN.sub("", source_html))
        return _STYLE_TAG_PATTERN.sub("", fragment).strip()

    @staticmethod
    def _transform_fragment(
        fragment: str,
        rules: tuple[_CssRule, ...],
        *,
        line_height: float,
    ) -> str:
        transformer = _EmailMarkupTransformer(
            rules,
            line_height=line_height,
        )
        transformer.feed(fragment)
        transformer.close()
        return transformer.content()

    @classmethod
    def _parse_stylesheet(cls, stylesheet: str) -> tuple[tuple[_CssRule, ...], tuple[_CssRule, ...], tuple[str, ...]]:
        desktop_rules: list[_CssRule] = []
        mobile_rules: list[_CssRule] = []
        warnings: list[str] = []

        def parse_blocks(css: str, *, is_mobile: bool) -> None:
            cursor = 0
            while cursor < len(css):
                start = css.find("{", cursor)
                if start < 0:
                    break
                selector = css[cursor:start].strip()
                depth = 1
                end = start + 1
                while end < len(css) and depth:
                    if css[end] == "{":
                        depth += 1
                    elif css[end] == "}":
                        depth -= 1
                    end += 1
                if depth:
                    warnings.append("Eine unvollständige CSS-Regel wurde nicht übernommen.")
                    return
                body = css[start + 1:end - 1]
                cursor = end
                if not selector:
                    continue
                if selector.casefold().startswith("@media"):
                    if "max-width" not in selector.casefold():
                        warnings.append("Eine nicht mobile CSS-Medienregel wurde nicht übernommen.")
                    else:
                        parse_blocks(body, is_mobile=True)
                    continue
                if selector.startswith("@"):
                    warnings.append("Eine CSS-Sonderregel wurde nicht übernommen.")
                    continue
                selectors = tuple(
                    candidate.strip()
                    for candidate in selector.split(",")
                    if _SIMPLE_SELECTOR_PATTERN.fullmatch(candidate.strip())
                )
                if not selectors:
                    warnings.append("Eine komplexe CSS-Auswahl wurde nicht übernommen.")
                    continue
                declarations = tuple(_EmailMarkupTransformer._safe_declarations(body).items())
                if not declarations:
                    warnings.append("Eine CSS-Regel ohne unterstützte Eigenschaften wurde nicht übernommen.")
                    continue
                rule = _CssRule(selectors=selectors, declarations=declarations, is_mobile=is_mobile)
                (mobile_rules if is_mobile else desktop_rules).append(rule)

        parse_blocks(_CSS_COMMENT_PATTERN.sub("", stylesheet), is_mobile=False)
        return tuple(desktop_rules), tuple(mobile_rules), tuple(dict.fromkeys(warnings))

    @staticmethod
    def _render_mobile_styles(rules: tuple[_CssRule, ...]) -> str:
        lines = [
            "      .hub-email-fluid-container, .hub-email-fluid-element { box-sizing: border-box !important; width: 100% !important; max-width: 100% !important; min-width: 0 !important; }"
        ]
        for rule in rules:
            selectors = ", ".join(rule.selectors)
            declarations = "; ".join(f"{name}: {value} !important" for name, value in rule.declarations)
            lines.append(f"      {selectors} {{ {declarations}; }}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_stylesheet_rules(rules: tuple[_CssRule, ...]) -> str:
        return "\n".join(
            f"{', '.join(rule.selectors)} {{ {'; '.join(f'{name}: {value}' for name, value in rule.declarations)}; }}"
            for rule in rules
        )

    @staticmethod
    def _outlook_list_styles(source: str) -> str:
        styles: list[str] = []
        if re.search(r"<ul\b", source, flags=re.IGNORECASE):
            styles.append("ul { margin: 0; padding: 0; } ul li { margin-left: 27px; mso-special-format: bullet; }")
        if re.search(r"<ol\b", source, flags=re.IGNORECASE):
            styles.append("ol { margin: 0; padding: 0; } ol li { margin-left: 27px; }")
        if not styles:
            return ""
        return "  <!--[if mso]><style type=\"text/css\">" + " ".join(styles) + "</style><![endif]-->\n"

    @staticmethod
    def _compiled_warnings(compiled_html: str) -> tuple[str, ...]:
        if len(compiled_html.encode("utf-8")) >= _GMAIL_CLIP_WARNING_THRESHOLD_BYTES:
            return (
                "Das Versand-HTML ist sehr groß und kann in Gmail abgeschnitten dargestellt werden.",
            )
        return ()

    @staticmethod
    def _preflight_warnings(source: str, stylesheet: str, stylesheet_warnings: tuple[str, ...]) -> tuple[str, ...]:
        warnings = [*stylesheet_warnings, *_EmailMarkupValidator.warnings_for(source)]
        normalized = f"{source}\n{stylesheet}".casefold()
        if "url(" in normalized or "background-image" in normalized:
            warnings.append("Hintergrundbilder werden nicht zuverlässig in allen E-Mail-Programmen dargestellt.")
        if any(token in normalized for token in ("display: flex", "display:flex", "display: grid", "display:grid", "position: fixed", "position:fixed", "float:")):
            warnings.append("Flex-, Grid-, Float- oder Fixed-Layout wird von einzelnen E-Mail-Programmen nicht zuverlässig unterstützt.")
        if _FIXED_WIDTH_PATTERN.search(source):
            warnings.append("Breite Elemente erhalten einen mobilen Fallback; komplexe Tabellen sollten zusätzlich in Gmail und Outlook geprüft werden.")
        if re.search(r"<img\b(?![^>]*\balt\s*=)[^>]*>", source, flags=re.IGNORECASE):
            warnings.append("Mindestens ein Bild hat keinen Alternativtext.")
        if any(
            not _IMAGE_WIDTH_ATTRIBUTE_PATTERN.search(match.group("attributes"))
            and not _IMAGE_WIDTH_STYLE_PATTERN.search(match.group("attributes"))
            for match in _IMG_TAG_PATTERN.finditer(source)
        ):
            warnings.append("Mindestens ein Bild hat keine feste Breite; große Bilder sollten eine Breite erhalten.")
        if _LONG_URL_PATTERN.search(source):
            warnings.append("Eine sehr lange URL kann in Outlook das Tabellenlayout verbreitern.")
        return tuple(dict.fromkeys(warnings))

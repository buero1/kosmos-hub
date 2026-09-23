"""Input contracts derived from the existing PDF/terms editors."""
from app.services.hub_operations import HubOperationInputField as Field

NAME_MIN_LENGTH = 3
NAME_FIELD = Field("name", "Bezeichnung", required=True, max_length=255)
HTML_FIELD = Field("content_html", "Inhalt (HTML)", max_length=500_000)
VERSION_FIELD = Field("expected_version", "Gelesene Version; veraltete Aenderungen ablehnen")


def pdf_fields(action):
    from app.services.hub_pdf_templates import PDF_TEMPLATE_TYPES, PDF_TEMPLATE_BLOCKS
    target = (Field("template_id", "PDF-Vorlagen-ID", required=True, max_length=18), VERSION_FIELD)
    if action == "create":
        return (NAME_FIELD, Field("document_type", "Belegart", required=True,
            options=tuple((item.key, item.label) for item in PDF_TEMPLATE_TYPES)))
    if action == "rename":
        return (*target, NAME_FIELD)
    if action == "update_block":
        return (*target, Field("block_key", "Vorlagenbereich", required=True,
            options=tuple((item.key, item.label) for item in PDF_TEMPLATE_BLOCKS)), HTML_FIELD,
            Field("is_visible", "Sichtbar", options=(("true", "Ja"), ("false", "Nein"))))
    if action == "set_legal_terms":
        return (*target, Field("legal_terms_id", "AGB-ID; leer entfernt die Zuordnung", max_length=18))
    if action == "update_positions":
        return (*target, Field("columns", "Vollstaendige Spaltenliste aus der gelesenen Vorlage", required=True,
            max_length=20_000, encoding="JSON array: key, label, source_key, width, alignment, is_enabled (boolean)"),
            Field("show_totals", "Summenbereich (Netto, USt., Gesamt) anzeigen", options=(("true", "Ja"), ("false", "Nein"))))
    return target


def legal_fields(action):
    if action == "create":
        return (NAME_FIELD,)
    target = (Field("legal_terms_id", "AGB-ID", required=True, max_length=18), VERSION_FIELD)
    if action == "update":
        return (*target, Field("name", NAME_FIELD.label, max_length=NAME_FIELD.max_length), HTML_FIELD)
    return target

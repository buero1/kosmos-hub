from pathlib import Path
import re

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.core.templates import create_templates
from app.db.base import Base
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_position_preset import HubFinancePositionPreset
from app.services.hub_finance_position_presets import (
    INVOICE_POSITION_PRESET_LIBRARY,
    SALES_POSITION_PRESET_LIBRARY,
    HubFinancePositionPresetError,
    HubFinancePositionPresetService,
)


def _line(*, article_id: int | None = None, price: str = "44,99") -> dict[str, str]:
    return {
        "article_id": str(article_id or ""),
        "name": 'Monatsbeitrag Homepage "Basic"',
        "sku": "1234",
        "description": "Homepage-Paket",
        "quantity": "1,00",
        "unit": "Monatlich",
        "unit_price": price,
        "discount_percent": "0",
        "tax_rate": "19",
    }


def test_sales_presets_are_shared_while_invoice_presets_are_separate_and_encrypted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("preset-tests")

    with Session(engine) as db:
        article = HubFinanceArticle(encrypted_fields_json=cipher.encrypt("{}"))
        db.add(article)
        db.flush()
        service = HubFinancePositionPresetService(db=db, cipher=cipher)
        sales = service.create(
            library_key=SALES_POSITION_PRESET_LIBRARY,
            name="Website Paket Basic",
            lines=[_line(article_id=article.id)],
            actor_username="kosmosadmin",
        )
        invoice = service.create(
            library_key=INVOICE_POSITION_PRESET_LIBRARY,
            name="Website Paket Basic",
            lines=[_line(article_id=article.id, price="49,99")],
            actor_username="kosmosadmin",
        )
        db.commit()

        stored = db.scalar(select(HubFinancePositionPreset).where(HubFinancePositionPreset.id == sales.id))
        assert stored is not None
        assert "Homepage-Paket" not in stored.encrypted_lines_json
        assert [preset.name for preset in service.list_presets(library_key=SALES_POSITION_PRESET_LIBRARY)] == ["Website Paket Basic"]
        assert service.list_presets(library_key=SALES_POSITION_PRESET_LIBRARY)[0].lines[0]["unit_price"] == "44.99"
        assert service.list_presets(library_key=INVOICE_POSITION_PRESET_LIBRARY)[0].id == invoice.id
        assert service.list_presets(library_key=INVOICE_POSITION_PRESET_LIBRARY)[0].lines[0]["unit_price"] == "49.99"


def test_duplicate_names_are_rejected_per_library_and_delete_is_scoped():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("preset-tests")

    with Session(engine) as db:
        service = HubFinancePositionPresetService(db=db, cipher=cipher)
        preset = service.create(
            library_key=SALES_POSITION_PRESET_LIBRARY,
            name="Website Paket Basic",
            lines=[_line()],
            actor_username="kosmosadmin",
        )
        db.commit()

        with pytest.raises(HubFinancePositionPresetError, match="bereits"):
            service.create(
                library_key=SALES_POSITION_PRESET_LIBRARY,
                name="website paket basic",
                lines=[_line()],
                actor_username="kosmosadmin",
            )
        db.rollback()

        with pytest.raises(HubFinancePositionPresetError, match="nicht gefunden"):
            service.delete(preset_id=preset.id, library_key=INVOICE_POSITION_PRESET_LIBRARY)

        service.delete(preset_id=preset.id, library_key=SALES_POSITION_PRESET_LIBRARY)
        db.commit()
        assert service.list_presets(library_key=SALES_POSITION_PRESET_LIBRARY) == ()


def test_deleted_articles_become_free_text_without_changing_the_saved_snapshot():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("preset-tests")

    with Session(engine) as db:
        article = HubFinanceArticle(encrypted_fields_json=cipher.encrypt("{}"))
        db.add(article)
        db.flush()
        service = HubFinancePositionPresetService(db=db, cipher=cipher)
        service.create(
            library_key=SALES_POSITION_PRESET_LIBRARY,
            name="Website Paket Basic",
            lines=[_line(article_id=article.id)],
            actor_username="kosmosadmin",
        )
        db.commit()

        db.delete(article)
        db.commit()
        line = service.list_presets(library_key=SALES_POSITION_PRESET_LIBRARY)[0].lines[0]
        assert line["article_id"] == ""
        assert line["name"] == 'Monatsbeitrag Homepage "Basic"'
        assert line["unit_price"] == "44.99"


def test_position_preset_drawer_renders_load_and_save_controls():
    rendered = create_templates(directory="app/templates").get_template(
        "partials/finance_position_preset_drawer.html"
    ).render(
        csrf_token="csrf",
        finance_position_preset_library_key=SALES_POSITION_PRESET_LIBRARY,
        finance_position_preset_library_label="Angebote & Aufträge",
        finance_position_presets=(),
        finance_position_presets_json=[],
    )

    assert "Vorlage laden" in rendered
    assert "Vorlage auswählen" in rendered
    assert "Gespeichertes Paket" not in rendered
    assert "Vorlage \"' + preset.name + '\" entfernen?" in rendered
    assert 'data-finance-position-preset-mode="load"' in rendered
    assert 'data-finance-position-preset-mode="save" hidden' in rendered
    assert "Aktuelle Positionen speichern" in rendered
    assert "data-finance-position-preset-load" in rendered
    assert "finance:position-preset-load" in rendered


@pytest.mark.parametrize(
    "template_name,row_attribute,insert_attribute,reindex_expression",
    (
        (
            "partials/finance_offer_positions.html",
            "data-finance-position-row",
            "data-finance-position-insert",
            "offer_line__' + index + '__",
        ),
        (
            "partials/finance_document_positions.html",
            "data-finance-document-position-row",
            "data-finance-document-position-insert",
            "document_line__' + index + '__",
        ),
    ),
)
def test_editable_finance_positions_render_drag_handles_and_persisted_reindexing(
    template_name: str,
    row_attribute: str,
    insert_attribute: str,
    reindex_expression: str,
):
    rendered = create_templates(directory="app/templates").get_template(template_name).render(
        line_rows=[_line()],
        articles=(),
        finance_positions_form_id="",
        finance_position_preset_library_key=SALES_POSITION_PRESET_LIBRARY,
    )

    assert row_attribute in rendered
    assert rendered.count("data-finance-position-drag-handle") == 3
    assert rendered.count('draggable="true"') == 2
    assert "rows.addEventListener('dragstart'" in rendered
    assert "rows.addEventListener('dragover'" in rendered
    assert "rows.addEventListener('dragend'" in rendered
    assert "finance-position-sortable-ghost" in rendered
    assert "animateRowPositions(previousPositions)" in rendered
    assert rendered.count(insert_attribute) == 3
    assert "appendRow(null, sourceRow)" in rendered
    assert "finance-position-unit" in rendered
    unit_selects = re.findall(r'<select\b[^>]*data-finance-(?:document-)?line-field="unit"[^>]*>', rendered)
    assert len(unit_selects) == 2
    assert all("required" not in select for select in unit_selects)
    assert '>Keine Einheit</option>' in rendered
    assert '>Monatlich</option>' in rendered
    assert '>Einmalig</option>' in rendered
    assert '>Jährlich</option>' in rendered
    assert '<th class="finance-position-net-heading">Netto</th>' in rendered
    assert 'data-finance-position-preset-open="load">Laden</button>' in rendered
    assert 'data-finance-position-preset-open="save">Speichern</button>' in rendered
    assert ">Positionspakete</button>" not in rendered
    if template_name == "partials/finance_offer_positions.html":
        assert '<p class="eyebrow">Angebote</p>' in rendered
        assert '<p class="eyebrow">Repeater</p>' not in rendered
    assert "Artikel, Menge, Tarif, USt. und Betrag werden" not in rendered
    assert reindex_expression in rendered
    assert "finance:position-edit-cancel" in rendered


@pytest.mark.parametrize(
    ("template_name", "search_attribute"),
    (
        ("partials/finance_offer_positions.html", "data-finance-article-search"),
        ("partials/finance_document_positions.html", "data-finance-document-article-search"),
    ),
)
def test_finance_article_search_uses_shared_dropdown_in_existing_and_new_rows(template_name: str, search_attribute: str):
    rendered = create_templates(directory="app/templates").get_template(template_name).render(
        line_rows=[_line()],
        articles=(),
        finance_positions_form_id="",
        finance_position_preset_library_key=SALES_POSITION_PRESET_LIBRARY,
    )

    assert rendered.count('aria-controls="finance-article-search-options"') == 2
    assert rendered.split("<script>", 1)[0].count(search_attribute) == 2
    assert 'list="finance-offer-article-suggestions"' not in rendered
    assert 'list="finance-document-article-suggestions"' not in rendered
    assert "Keine Artikel gefunden. Freitext ist möglich." in rendered
    assert "option.dataset.articleId" in rendered


def test_finance_position_tables_fit_their_panel_in_read_and_edit_modes():
    base_template = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert ".finance-position-net-heading { width: 100px !important; }" in base_template
    assert "[data-finance-position-preset-save] { width: min(240px, 100%); }" in base_template
    assert ".finance-positions-panel > .table-scroll { margin-top: 20px; }" in base_template
    assert "[data-customer-edit-position-readonly] .finance-positions-table { width: 100%; min-width: 0; table-layout: fixed; }" in base_template
    assert "[data-customer-edit-position-readonly] .finance-positions-table th:nth-child(2) { width: 134px; }" in base_template
    assert "[data-customer-edit-position-readonly] .finance-positions-table th:nth-child(5) { width: 100px; }" in base_template


def test_read_only_finance_position_panels_have_no_preset_controls():
    offer_detail = Path("app/templates/finance_offer_detail.html").read_text(encoding="utf-8")
    document_detail = Path("app/templates/finance_document_detail.html").read_text(encoding="utf-8")

    assert "data-finance-position-preset-open" not in offer_detail
    assert "data-finance-position-preset-open" not in document_detail

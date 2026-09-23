from pathlib import Path


def test_dunning_email_panel_is_below_positions_and_uses_composer_context():
    detail = Path("app/templates/finance_document_detail.html").read_text(encoding="utf-8")
    positions_end = detail.index('{% if module.is_dunning and can_view_dunning_emails %}')
    positions_heading = detail.index("<h3>Positionen</h3>")
    positions_editor = detail.index('{% include "partials/finance_document_positions.html" %}')

    assert positions_heading < positions_end < positions_editor

    panel = Path("app/templates/partials/finance_dunning_email_panel.html").read_text(encoding="utf-8")
    assert "E-Mail schreiben" in panel
    assert 'data-email-compose-dunning-id="{{ detail.document.id }}"' in panel
    assert 'data-email-compose-customer-id="{{ detail.document.customer_id }}"' in panel
    assert "Geplant" in panel

    composer = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert 'dunning_id: globalMailboxDunningId.value' in composer

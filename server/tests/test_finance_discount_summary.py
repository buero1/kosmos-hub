from pathlib import Path


def test_finance_summary_discount_row_is_hidden_everywhere():
    offer_detail = Path("app/templates/finance_offer_detail.html").read_text(encoding="utf-8")
    document_detail = Path("app/templates/finance_document_detail.html").read_text(encoding="utf-8")
    offer_editor = Path("app/templates/partials/finance_offer_positions.html").read_text(encoding="utf-8")
    document_editor = Path("app/templates/partials/finance_document_positions.html").read_text(encoding="utf-8")
    pdf = Path("app/templates/finance_generated_pdf.html").read_text(encoding="utf-8")

    assert "<dt>Rabatt</dt>" not in offer_detail
    assert "<dt>Rabatt</dt>" not in document_detail
    assert 'data-finance-total="discount"' not in offer_editor
    assert 'data-finance-document-total="discount"' not in document_editor
    assert "<th>Rabatt</th>" not in pdf

    # Existing line discounts stay editable so historic records keep their totals.
    assert 'data-finance-line-field="discount_percent"' in offer_editor
    assert 'data-finance-document-line-field="discount_percent"' in document_editor

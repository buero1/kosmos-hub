from decimal import Decimal
import re

from lxml import html as html_parser
import pytest

from app.api.routes import web
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_documents import FINANCE_DOCUMENT_MODULES, HubFinanceDocumentService
from test_hub_finance_operations import env, values


@pytest.mark.parametrize("kind, currency, symbol", [
    (kind, currency, symbol)
    for kind in ("offers", *FINANCE_DOCUMENT_MODULES)
    for currency, symbol in (("EUR", "\u20ac"), ("CHF", "CHF"), ("USD", "USD"))
    if kind != "orders" or currency == "EUR"
])
def test_saved_position_panel_formats_prices_and_totals_with_document_currency(env, kind, currency, symbol):
    prefix = "offer" if kind == "offers" else "document"
    data = values(env, kind)
    if kind != "orders":
        data[f"{prefix}_field__currency"] = currency
    record_id = env.service.execute(f"finance.{kind}.create", data).record_id
    env.db.commit()
    if kind == "offers":
        detail = HubFinanceService(db=env.db, cipher=env.cipher).get_offer_detail(offer_id=record_id)
        module = None
        template = "finance_offer_detail.html"
    else:
        module = FINANCE_DOCUMENT_MODULES[kind]
        detail = HubFinanceDocumentService(db=env.db, cipher=env.cipher).get_detail(module=module, document_id=record_id)
        template = "finance_document_detail.html"
    if kind != "orders":
        assert next(field.form_value for field in detail.fields if field.key == "currency") == currency
    assert detail.totals.currency == currency
    assert detail.lines[0].unit_price == "100.00"
    assert detail.lines[0].unit_price_display == "100,00 " + symbol
    assert detail.lines[0].amount_net == Decimal("100")
    assert detail.lines[0].amount_net_display == "100,00 " + symbol
    source, _, _ = web.templates.env.loader.get_source(web.templates.env, template)
    panel = re.search(r'<article\b[^>]*data-customer-edit-position-readonly\b.*?</article>', source, re.S).group()
    rendered = web.templates.env.from_string(panel).render(detail=detail, module=module)
    dom = html_parser.fromstring(rendered)
    for row in dom.xpath('.//tbody/tr'):
        for column in (3, 5):
            assert row.xpath(f'./td[{column}]')[0].text_content().strip().endswith(symbol)
    for total in dom.xpath('.//dl[contains(@class,"finance-totals")]//dd'):
        assert total.text_content().strip().endswith(symbol)
    if currency == "EUR":
        assert "EUR" not in dom.text_content()
    else:
        assert "\u20ac" not in dom.text_content()


@pytest.mark.parametrize("currency, suffix", [("EUR", "\u20ac"), ("CHF", "CHF"), ("USD", "USD")])
def test_shared_finance_formatter_only_changes_euro_display(currency, suffix):
    assert HubFinanceService.format_money(Decimal("1234.565"), currency) == "1.234,57 " + suffix

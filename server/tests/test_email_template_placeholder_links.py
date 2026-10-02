from app.services.customer_communications import CustomerCommunicationService


def test_legacy_customer_website_placeholder_link_is_normalized_and_preserved():
    content = CustomerCommunicationService._sanitized_email_content(
        '<p><a href="${customer website}">${customer website}</a></p>',
        allow_template_href_placeholders=True,
    )

    assert content == '<p><a href="${Customer.Website}">${Customer.Website}</a></p>'

from types import SimpleNamespace

from app.services.site_selection import build_site_selector_context


def _site(site_id: int, status: str | None) -> SimpleNamespace:
    customer = None if status is None else SimpleNamespace(
        id=site_id,
        name=f"Customer {site_id}",
        zoho_status=status,
    )
    return SimpleNamespace(id=site_id, customer=customer)


def test_site_selector_only_includes_operational_customer_statuses():
    context = build_site_selector_context(
        action="/updates",
        form_id="site-scope",
        sites=[
            _site(1, "Aktuell"),
            _site(2, "Neu"),
            _site(3, "Kündigung liegt vor"),
            _site(4, "gekündigt"),
            _site(5, "Sonstiges"),
            _site(6, None),
        ],
        selected_site_ids={1, 4, 6},
        site_scope="selected",
        submit_label="Show updates",
    )

    assert [site.id for site in context["sites"]] == [1, 2, 3]
    assert context["selected_site_ids"] == {1}
    assert [customer["status"] for customer in context["customers"]] == ["Aktuell", "Neu", "Kündigung liegt vor"]


def test_site_selector_all_means_all_selectable_sites():
    context = build_site_selector_context(
        action="/updates",
        form_id="site-scope",
        sites=[_site(1, "Aktuell"), _site(2, "gekündigt")],
        selected_site_ids=None,
        site_scope="all",
        submit_label="Show updates",
    )

    assert context["site_scope"] == "selected"
    assert context["selected_site_ids"] == {1}


def test_site_selector_can_expose_update_display_modes():
    context = build_site_selector_context(
        action="/updates",
        form_id="site-scope",
        sites=[_site(1, "Aktuell")],
        selected_site_ids={1},
        site_scope="selected",
        submit_label="Show updates",
        csrf_token="csrf-token",
        secondary_submit_action="/updates/fresh-show",
        secondary_primary_label="Show stored updates",
        secondary_submit_label="Show fresh updates",
        protocol_submit_label="Show refresh protocol",
        selected_display_mode="protocol",
    )

    assert context["secondary_submit_action"] == "/updates/fresh-show"
    assert context["secondary_primary_label"] == "Show stored updates"
    assert context["secondary_submit_label"] == "Show fresh updates"
    assert context["protocol_submit_label"] == "Show refresh protocol"
    assert context["selected_display_mode"] == "protocol"

from typing import Any


def build_site_selector_context(
    *,
    action: str,
    form_id: str,
    sites: list[Any],
    selected_site_ids: set[int] | None,
    site_scope: str,
    submit_label: str,
    target_form_id: str = "",
    hide_submit: bool = False,
) -> dict[str, Any]:
    """Build one reusable domain/customer selector for fleet-facing pages."""
    customers: dict[int, dict[str, Any]] = {}
    for site in sites:
        customer = site.customer
        if customer is None:
            continue
        customer_entry = customers.setdefault(
            customer.id,
            {
                "id": customer.id,
                "name": customer.name,
                "status": customer.zoho_status or "",
                "site_ids": [],
            },
        )
        customer_entry["site_ids"].append(site.id)

    return {
        "action": action,
        "form_id": form_id,
        "sites": sites,
        "customers": sorted(customers.values(), key=lambda entry: entry["name"].casefold()),
        "selected_site_ids": selected_site_ids or set(),
        "site_scope": site_scope,
        "submit_label": submit_label,
        "target_form_id": target_form_id,
        "hide_submit": hide_submit,
    }

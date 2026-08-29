from typing import Any


SELECTABLE_CUSTOMER_STATUSES = frozenset({"Aktuell", "Neu", "Kündigung liegt vor"})


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
    method: str = "get",
    csrf_token: str = "",
    secondary_submit_action: str = "",
    secondary_submit_label: str = "",
) -> dict[str, Any]:
    """Build one reusable domain/customer selector for fleet-facing pages."""
    selectable_sites = [
        site
        for site in sites
        if site.customer is not None and site.customer.zoho_status in SELECTABLE_CUSTOMER_STATUSES
    ]
    selectable_site_ids = {site.id for site in selectable_sites}
    # "All" in an operational selector means all currently selectable websites,
    # not every historical or unlinked site stored in the Hub.
    effective_selected_site_ids = (
        selectable_site_ids
        if site_scope == "all"
        else (selected_site_ids or set()) & selectable_site_ids
    )
    customers: dict[int, dict[str, Any]] = {}
    for site in selectable_sites:
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
        "sites": selectable_sites,
        "customers": sorted(customers.values(), key=lambda entry: entry["name"].casefold()),
        "selected_site_ids": effective_selected_site_ids,
        "site_scope": "selected",
        "submit_label": submit_label,
        "target_form_id": target_form_id,
        "hide_submit": hide_submit,
        "method": method,
        "csrf_token": csrf_token,
        "secondary_submit_action": secondary_submit_action,
        "secondary_submit_label": secondary_submit_label,
    }

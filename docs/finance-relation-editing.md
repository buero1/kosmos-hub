# Existing Finance relations in edit forms

Finance's new-customer picker excludes customers with is_visible=false. Existing
documents can still reference them and be accessible under current record access
rules. Previously their names appeared read-only, but the picker omitted the
stored customer and consequently all its contact options in edit mode.

finance_options now accepts an optional record_id. It first authorizes that
Finance record, then includes its existing customer in the ordinary options if
necessary. Contacts remain limited to the allowed customers. Other filtered or
unauthorized customers are not added. No customer flags or stored relations change.

Offer and document detail forms use this context. The shared finance.options
query exposes the same optional record_id for parity with the agent. New-record
pickers retain their previous filtering.

Cancel/reset restores the original hidden customer/lead IDs explicitly (assigning
hidden input values also changes their native reset default), then re-filters
contact and document options after the browser restores ordinary form values.
This also restores required/disabled contact state in
the customer/lead picker, preventing a disabled original contact from disappearing
from the next save after an abandoned customer change.

Regression coverage renders five Finance modules, checks read scope and saved
relations, and opens/cancels/reopens their real browser forms at 1263/390px.
Production verification is read-only; no recurring dates, invoice records or
customer visibility flags are changed by this release.

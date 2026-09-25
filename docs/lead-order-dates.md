# Lead order dates

The existing `lead-result-field-updates` workflow now sets `order_date` and
`billing_result_date` when the main Lead result changes to the displayed option
"Auftrag" (stored key `Vertrag`) or `Stattgefunden + Auftrag`. Dates are stored as
ISO dates. Existing values, including values entered in the same save, take priority.

- Populated dates are preserved, even when the two dates differ.
- An empty order date receives the current Berlin calendar date, including across
  UTC midnight and DST boundaries. An empty billing-result date takes the order date.
- If only the billing-result date is populated, it is preserved and the empty order
  date receives the Berlin change date. If both are empty, both receive that date.
- Subsequent unrelated saves do not overwrite or backfill dates.
- A later transition away and back also preserves populated dates. Clearing both
  dates explicitly when changing the result uses the new transition date again.
- Disabling the existing workflow disables the new date updates as well.
- Other results retain their existing behavior.
- No historical Lead data is backfilled. Customer creation is handled separately by
  the [Lead conversion workflow](lead-customer-conversion.md), not by this date rule.
- The shared workflow is used by Lead creation, editing and external upserts.

Coverage: `tests/test_lead_order_dates.py`, `tests/test_hub_leads.py`,
`tests/test_hub_record_operations.py` and `tests/test_hub_architecture_contracts.py`.

# Lead order dates

The existing `lead-result-field-updates` workflow now sets `order_date` and
`billing_result_date` when the main Lead result changes to the displayed option
"Auftrag" (stored key `Vertrag`) or `Stattgefunden + Auftrag`. Both dates use the current Berlin calendar date
and are stored as ISO dates, including across UTC midnight and DST boundaries.

- Existing date values are replaced on an actual transition to Auftrag.
- Subsequent unrelated saves do not overwrite or backfill dates.
- A later transition away and back to Auftrag records the new transition date.
- Disabling the existing workflow disables the new date updates as well.
- Other results retain their existing behavior.
- No historical Lead data is backfilled. Customer creation is handled separately by
  the [Lead conversion workflow](lead-customer-conversion.md), not by this date rule.
- The shared workflow is used by Lead creation, editing and external upserts.

Coverage: `tests/test_lead_order_dates.py`, `tests/test_hub_leads.py`,
`tests/test_hub_record_operations.py` and `tests/test_hub_architecture_contracts.py`.

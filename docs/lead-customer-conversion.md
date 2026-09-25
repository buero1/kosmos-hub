# Lead-to-customer workflow

`lead-customer-conversion` is enabled by default and independently switchable in
Settings / Workflows. Trigger: a real change of the main Lead result to `Vertrag`
(displayed as Auftrag) or `Stattgefunden + Auftrag`. The existing field-update
workflow stamps both order/billing-result dates in Europe/Berlin for either result.

## Transaction and mapping

- A Hub create with an order result and an existing external Lead's result change
  use the same service. First-time external imports and unchanged results never
  retroactively convert historical orders.
- One savepoint contains Lead changes, customer/contact creation, note copies,
  activity reassignment, access metadata and conversion tracking. Failures roll
  back all of those changes even if a caller subsequently commits.
- The Lead row is locked before reading its previous fields. A unique conversion
  record provides additional protection against duplicate execution. Deleting a
  target leaves the conversion record intact; subsequent saves do not recreate it.
- Customer: company, telephone, website, industry, billing street/postal code/city/
  country, source and order date. Status is Neu; customer type is Kunde.
- Contact: salutation, letter salutation, first/last name, position as function,
  both email addresses, telephone/mobile/alternate telephone and postal address.
  Frau/Herr Dr. is split into salutation and title.
- Company and contact-required fields must be present. Possible existing customers
  (same company or website) or contacts (matching email) stop conversion with a safe
  error; there is no automatic merge or overwrite and no disclosure of hidden IDs.

## History and access

- Notes are independent encrypted copies with original authorship and timestamps,
  not remote Zoho links. The original Lead and its notes remain unchanged.
- Calls/tasks/meetings retain IDs, schedules, assignees, status and reminders. Only
  their CRM parent changes. Snoozes and email retry state survive. A reminder that
  is currently sending blocks conversion until delivery has finished.
- Existing Lead mail is neither moved nor copied. Address-based associations for
  converted customers/contacts start at conversion, including for historical mail
  imported afterwards. New correspondence follows normal address matching.
- Role-level customer/contact view and create rights are required for an identified
  actor. Customer ownership/team and explicit grants follow the Lead; contact access
  follows customer access. Activity assignees retain already-authorized access when
  their customer module permissions allow it. Module rights are never expanded.
- The shared Hub overview displays conversion date, actor snapshot and the other
  record's link, with ordinary record-level visibility checks.
- No bidirectional synchronization, finance-document creation, live email sending
  or historical-data backfill is part of this workflow.

## Schema and verification

`hub_lead_conversions` is an additive table created idempotently at startup, also
when automatic full schema creation is disabled. Foreign keys use SET NULL so
deleting either record does not delete the counterpart or conversion evidence.

Tests: `test_lead_conversion.py`, `test_lead_order_dates.py`, and the existing CRM,
email-association, access, activity/reminder, deletion and workflow test suites.
No production Lead is converted for verification.

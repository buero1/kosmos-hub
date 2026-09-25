# Invoice email delivery display

Invoices display two read-only values in the directory and detail sidebar:
`Versandstatus` and `Versendet am`. They are independent of invoice/payment status.

- Status follows the latest confirmed invoice dispatch attempt: not sent, queued,
  sending, sent, failed, or uncertain. Unknown states display as uncertain.
- The date is the latest successful SMTP handoff, stored explicitly in UTC and
  displayed in Berlin local time, including daylight saving, without a suffix.
- Failed or uncertain resends do not erase a previous successful send time.
- Successful dispatch means mail transport accepted the message, not delivered/read.
- The summary queries the existing invoice batch journal in one query per page.
  It does not decrypt recipient addresses or message bodies. Existing invoice
  record access checks remain in force.
- Batch polling updates both columns without a reload. A later reload and the
  detail page use the same server-side summary.
- Only tracked Hub invoice dispatches count. Manually attached PDFs, external
  emails and imported invoice statuses are not inferred as successful sends.

The additive nullable `hub_invoice_email_batch_items.sent_at` column is created
at startup. Legacy successful rows remain recognizable as sent, but without an
invented timestamp: their generic `updated_at` may be server-local or changed
for unrelated reasons. The production preflight found zero batch items before
this release, so no historical data backfill is needed.

No email is sent by installing this release. The guarded full release refuses
to restart the service while a queued or running invoice email batch exists.

# Manual invoice email

The invoice detail action prepares the existing global email drawer. It uses the
same "Rechnungen senden" template, designated invoice contact and finished
ZUGFeRD PDF as bulk invoice mail. Opening it does not send anything. A missing
template, recipient, sender permission or finished PDF produces an actionable
error instead of generating or dispatching mail in the background.

The prepared message is a normal encrypted Hub draft. Subject, body, recipient,
sender, CC and additional attachments remain editable. Reopening it in the mail
center preserves its invoice association. Invoice drafts are manual-only, without
scheduled delivery. Generic draft updates cannot remove their server-owned
invoice_dispatch metadata.

Both existing manual send endpoints delegate invoice drafts to the shared,
human-only finance.invoices.email.send operation. It checks current finance,
customer and mailbox access, invoice/PDF freshness and presence of the original
invoice PDF. It saves the latest submitted content and attachments before SMTP.
An invoice edited after preparation requires a new draft to avoid stale PDFs.

A durable sending marker is committed before SMTP. Concurrent/repeated submits
of the same draft cannot dispatch twice. Success records an explicit UTC sent_at
in the existing invoice delivery journal and discards the draft. Known failure
leaves it editable for an explicit retry; unknown outcome or interrupted process
blocks retry of that draft pending verification. A new independently prepared
draft is an explicit new dispatch, not deduplicated across different drafts.

The existing list/detail delivery fields reflect this journal. Invoice business
status is untouched. "Versendet" confirms successful SMTP submission, not final
recipient delivery or reading. No historical sends are inferred or fabricated.

Tests use encrypted temporary draft attachments and mocked SMTP only. Browser
fixtures run the real rendered invoice template and editor at desktop/mobile
sizes with local mocked HTTP endpoints. No customer email is sent by verification.

Release: full app archive via tools/deploy-lead-conversion.py --invoice-compose;
no schema change. Active background jobs and exact production baseline are
checked before the atomic app switch.

# Manual Order Email

- Orders have the same header button and shared right-hand composer as invoices.
- Preparing requires email view/create/edit, finance view, customer scope and a
  permitted sending mailbox. The existing active template named
  `Auftragsbestaetigung` (German umlaut in the UI) is rendered with the order,
  customer, selected contact and current sender signature context.
- The standard may be categorized as orders, customers or general, independent
  of its folder. An unambiguous orders-specific template takes precedence. A
  missing/ambiguous standard does not block the recipient/PDF: an unsent draft
  opens without preselected template, ready for manual template selection/text.
- The order's selected contact supplies the recipient. Without a selected
  contact, the first available customer recipient is proposed. Missing customer,
  recipient or PDF yields an actionable error, not an empty send.
- Missing template values remain editable in the draft with a warning. Sending
  is blocked until remaining `${...}` placeholders are completed or removed.
- The latest generated PDF is preferred. The imported original is used only
  when no generated PDF exists; pending/failed generation is not bypassed.
- Opening only stores an encrypted draft, never sends. Subject, content,
  recipient, sender and additional attachments remain editable. Reopened drafts
  retain the same order/customer metadata and PDF.
- Converted Lead offers are accepted as template sources only when the stored
  Lead conversion proves they belong to the order's customer. Source permissions
  still apply; unrelated customer/Lead documents remain rejected.
- Both normal send endpoints route prepared order drafts through the same
  human-only operation. The sent copy is explicitly stored as customer mail,
  including when a free recipient address is entered, and appears in customer
  communications subject to existing mailbox permissions. Customer data and the
  order's business status are not modified.
- Before SMTP, recheck permissions, unchanged order fields/lines/customer,
  current PDF bytes and inclusion of that PDF in the attachments. Persist a send
  claim before SMTP; confirmed failures may retry, unknown outcomes are blocked.
  No automatic scheduling or bulk sending is added.

## Verification

`tests/test_order_email_composer.py` uses SQLite and a fake SMTP transport.
`tests/js/invoice_composer.cjs --orders` uses rendered fixtures and mocked HTTP
at desktop/mobile sizes. Run without the flag for the existing invoice workflow.
No real email is sent by these tests. Deployment uses the full guarded app
release with `tools/deploy-lead-conversion.py --order-compose`.

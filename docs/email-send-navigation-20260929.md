# Repeated email send confirmation

The second successful send from a record can return the same path and query as
the current success page. Assigning that URL with a fragment only changes the
scroll position; it does not fetch a new document. The composer then stays in
its submitting state even though SMTP succeeded and the email was committed.

Both composers use shared navigation now: for the same origin/path/query,
preserve the target fragment and history state, then reload the document.
Other success targets navigate normally. Failed or uncertain sends preserve
the form; navigation never sends or retries an email. Asset version is bumped.

Verification: composer failure/success JavaScript tests, local Chrome fixture
reproducing the old behavior and testing same/different fragment and record,
plus email delivery, draft attachment and SEPA Python regression tests.
No real emails or SEPA forms are submitted during verification.

Deploy only the three frontend runtime files on the SEPA release baseline,
using the guarded deploy script with `--email-send-navigation`.

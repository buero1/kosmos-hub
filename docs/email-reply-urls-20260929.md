# Malformed Email URLs

The shared email composer sanitizer now discards malformed anchor URLs and image
sources instead of aborting reply/forward preparation with a URL parsing error.
Anchor text is preserved. Defanged domains (`example[.]org`) are never silently
reactivated. Existing allowed schemes, safe markup and valid IPv6 links remain
unchanged. Stored original messages are not modified and no emails are sent by
the fix or its verification.

Regression coverage includes malformed bracketed hosts, invalid IPv6/IPv4 host
forms, invalid Unicode authorities, valid links, disallowed schemes, and linked
customer/unassigned mailbox reply and forward flows.

Deployment uses the guarded `--email-reply-urls` release against the full prior
runtime snapshot, checks active jobs, and retains an application rollback copy.

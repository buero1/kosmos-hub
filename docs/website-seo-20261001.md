# Website SEO review

The Website detail page has a dedicated `SEO` section. It reads at most forty
published WordPress pages through Kosmos Bridge 0.3.71, including bounded visible
text and existing Yoast title and description values. Analysis never writes to
WordPress.

The configured OpenAI model receives only the displayed page inventory and the
non-sensitive site/customer names needed for context. Page content is treated as
untrusted source material. The model must return one title and description for
each exact page ID; duplicate, missing, foreign-domain, oversized, or malformed
results are rejected.

The user reviews and can edit every proposal before selecting pages and
confirming the write. Noindex pages are unselected by default. A one-hour
encrypted preview token binds the actor, site UUID, original SEO values, and page
revisions. Bridge preflights every selected revision before writing, verifies the
stored Yoast metadata, and rolls back the batch if a write cannot be confirmed.

A successful job offers a seven-day rollback. That rollback is also revision
protected, so it cannot overwrite later manual edits. Audit entries contain only
page IDs and outcome, never page content or SEO text.

The Hub release is built from the verified production release
`25102a7b94f97f55a5dc9e59554923198208e3b5`. The guarded deployment accepts only
the seven expected runtime changes, refuses deployment while background jobs are
active, verifies the exact previous runtime, keeps a full rollback copy, and
requires a healthy service after restart.

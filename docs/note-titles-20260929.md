# Optional Note Titles

Customer and lead notes no longer derive their title from the first content line.
An omitted title stays empty during creation and content-only edits. Titles are
optional during editing, so users can explicitly clear an existing heading.
Explicit titles are still supported. The customer/lead UI renders a heading only
when present; author/time metadata and note actions remain available. Note actions
stay right-aligned on desktop and mobile using the existing responsive layout.

The same rule applies to shared Hub operations and external lead-note upserts.
Original stored notes are not migrated or rewritten. Customer edits keep legacy
and local payload title keys consistent so a cleared title cannot reappear.

Tests cover normalization, validation, shared human/agent CRUD, title removal,
customer/lead rendering, external upsert behavior and retained note metadata.
The scoped `--note-titles` deployment verifies the full previous runtime and
checks active jobs before deployment, with application rollback available.

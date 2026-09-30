# Customer checklists

Customer detail pages now include a `Checklisten` field tab after the existing
profile tabs. Existing and newly created customers receive one initial `Design
Seiten` checklist with nine standard website-production tasks.

Authorized customer editors can:

- complete or reopen an item by clicking its checkbox or label;
- edit and delete individual items;
- add items below a checklist;
- create, rename, and delete checklist sections; and
- reorder checklist sections and their items by drag and drop.

Completed item text is struck through. A checklist is marked `Abgeschlossen`
when all its items are complete. Numeric progress is intentionally omitted.
Deleting an individual item is immediate; deleting a whole checklist keeps a
confirmation because it also removes all contained items.

Checklist writes require the existing customer edit permission, CSRF
validation, exact customer ownership, and an audit-log entry. The startup
schema step adds the two checklist tables and initializes the standard list
exactly once per customer. Deleting every checklist therefore does not recreate
the default on the next restart.

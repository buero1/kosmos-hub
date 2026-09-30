# Additional customer checklist defaults

The standard customer checklist template now contains three sections:

1. `Design Seiten` with 9 items
2. `Nach Kundensicht` with 6 items
3. `Letzte Einrichtungen` with 18 items

A persisted template version upgrades existing customers exactly once and gives
new customers all three sections. Existing checklist order and user-created
sections are preserved. A user may still rename or delete an added section; it
is not recreated after the template version was applied.

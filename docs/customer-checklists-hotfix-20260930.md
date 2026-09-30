# Customer checklist action URL hotfix

The customer detail context now passes its customer ID to the checklist partial.
This prevents checklist actions from submitting to `/customers//checklists/actions`.

Checkbox changes are reflected immediately. If a request fails, the checkbox and
line-through state return to their previous value instead of leaving a misleading
local state.

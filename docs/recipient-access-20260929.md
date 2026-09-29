# Recipient Access

The customer-specific email recipient picker and global recipient search now
use customer and contact permissions instead of the historical import visibility
flag or customer type. A permitted customer with status "Sonstige" and type
"Prospect" can be selected without modifying any stored status or profile.

The shared reader still requires an active actor with email access and customer
visibility. Customer record assignments/grants, contact permissions, and existing
mailbox/send permissions remain in force. Scope filtering still occurs before
search result limiting. No customer records or mail are changed by this release.

Regression tests cover both visibility flag values, admin/scoped users, direct
UI selection and shared search, missing or denied customers, disabled module
permissions, inactive users, and unchanged customer data.

The guarded `--recipient-access` deployment changes only two runtime services,
verifies the previous runtime snapshot and active jobs, and retains a rollback.

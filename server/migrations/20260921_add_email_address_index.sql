CREATE TABLE IF NOT EXISTS hub_email_addresses (
    id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_email_id INTEGER NULL,
    mailbox_email_id INTEGER NULL,
    address_digest VARCHAR(64) NOT NULL,
    CONSTRAINT ck_hub_email_addresses_one_source CHECK ((customer_email_id IS NULL) <> (mailbox_email_id IS NULL)),
    UNIQUE (customer_email_id, address_digest),
    UNIQUE (mailbox_email_id, address_digest),
    FOREIGN KEY (customer_email_id) REFERENCES customer_zoho_emails(id) ON DELETE CASCADE,
    FOREIGN KEY (mailbox_email_id) REFERENCES hub_mailbox_emails(id) ON DELETE CASCADE,
    INDEX ix_hub_email_addresses_customer_email_id (customer_email_id),
    INDEX ix_hub_email_addresses_mailbox_email_id (mailbox_email_id),
    INDEX ix_hub_email_addresses_address_digest (address_digest)
);

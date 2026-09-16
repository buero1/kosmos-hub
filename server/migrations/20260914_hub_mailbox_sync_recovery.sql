ALTER TABLE hub_mailbox_emails MODIFY COLUMN encrypted_payload_json MEDIUMTEXT NOT NULL;
ALTER TABLE hub_users ADD COLUMN mailbox_alert_email VARCHAR(320) NULL;

CREATE TABLE hub_mailbox_imap_sync_failures (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    mailbox_account_id INT NOT NULL,
    folder VARCHAR(128) NOT NULL,
    imap_uid VARCHAR(32) NOT NULL,
    error TEXT NOT NULL,
    attempts INT NOT NULL DEFAULT 1,
    last_failed_at DATETIME NOT NULL,
    resolved_at DATETIME NULL,
    alerted_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_hub_mailbox_sync_failure_uid UNIQUE (mailbox_account_id, folder, imap_uid),
    INDEX ix_hub_mailbox_sync_failures_resolved_at (resolved_at),
    INDEX ix_hub_mailbox_imap_sync_failures_mailbox_account_id (mailbox_account_id),
    CONSTRAINT fk_hub_mailbox_sync_failures_account FOREIGN KEY (mailbox_account_id)
        REFERENCES hub_mailbox_accounts (id) ON DELETE CASCADE
);

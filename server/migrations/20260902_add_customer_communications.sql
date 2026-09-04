CREATE TABLE IF NOT EXISTS customer_zoho_notes (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    zoho_note_id VARCHAR(255) NULL,
    source VARCHAR(16) NOT NULL,
    sync_status VARCHAR(16) NOT NULL DEFAULT 'synced',
    encrypted_payload_json TEXT NOT NULL,
    created_by_username VARCHAR(64) NULL,
    zoho_created_at DATETIME NULL,
    zoho_modified_at DATETIME NULL,
    zoho_synced_at DATETIME NULL,
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_customer_zoho_notes_customer_id FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE,
    CONSTRAINT uq_customer_zoho_notes_zoho_note_id UNIQUE (zoho_note_id),
    INDEX ix_customer_zoho_notes_customer_id (customer_id)
);

CREATE TABLE IF NOT EXISTS customer_zoho_emails (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    zoho_message_id VARCHAR(512) NULL,
    zoho_module VARCHAR(32) NULL,
    zoho_record_id VARCHAR(255) NULL,
    source VARCHAR(16) NOT NULL,
    direction VARCHAR(16) NOT NULL DEFAULT 'unknown',
    sync_status VARCHAR(16) NOT NULL DEFAULT 'synced',
    encrypted_payload_json TEXT NOT NULL,
    created_by_username VARCHAR(64) NULL,
    zoho_sent_at DATETIME NULL,
    zoho_synced_at DATETIME NULL,
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_customer_zoho_emails_customer_id FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE,
    CONSTRAINT uq_customer_zoho_emails_zoho_message_id UNIQUE (zoho_message_id),
    INDEX ix_customer_zoho_emails_customer_id (customer_id)
);

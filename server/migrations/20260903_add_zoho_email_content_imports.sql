CREATE TABLE IF NOT EXISTS zoho_email_content_imports (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    requested_by VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    requested_limit INT NOT NULL,
    total_emails INT NOT NULL DEFAULT 0,
    processed_emails INT NOT NULL DEFAULT 0,
    loaded_emails INT NOT NULL DEFAULT 0,
    failed_emails INT NOT NULL DEFAULT 0,
    started_at DATETIME NULL,
    completed_at DATETIME NULL,
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_zoho_email_content_imports_status (status)
);

ALTER TABLE customer_zoho_emails
    MODIFY COLUMN encrypted_payload_json MEDIUMTEXT NOT NULL;

CREATE TABLE IF NOT EXISTS zoho_email_content_import_items (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    email_import_id INT NOT NULL,
    email_id INT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_zoho_email_content_import_items_import
        FOREIGN KEY (email_import_id) REFERENCES zoho_email_content_imports (id) ON DELETE CASCADE,
    CONSTRAINT fk_zoho_email_content_import_items_email
        FOREIGN KEY (email_id) REFERENCES customer_zoho_emails (id) ON DELETE CASCADE,
    CONSTRAINT uq_zoho_email_content_import_items_import_email UNIQUE (email_import_id, email_id),
    INDEX ix_zoho_email_content_import_items_import_id (email_import_id),
    INDEX ix_zoho_email_content_import_items_email_id (email_id),
    INDEX ix_zoho_email_content_import_items_status (status)
);

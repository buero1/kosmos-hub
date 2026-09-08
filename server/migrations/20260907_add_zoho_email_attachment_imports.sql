CREATE TABLE IF NOT EXISTS customer_email_attachments (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    email_id INT NOT NULL,
    source VARCHAR(16) NOT NULL DEFAULT 'zoho',
    source_attachment_id VARCHAR(255) NOT NULL,
    storage_key VARCHAR(96) NOT NULL,
    content_type VARCHAR(128) NOT NULL DEFAULT 'application/octet-stream',
    byte_size INT NOT NULL,
    stored_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_customer_email_attachments_email_source UNIQUE (email_id, source_attachment_id),
    CONSTRAINT uq_customer_email_attachments_storage_key UNIQUE (storage_key),
    CONSTRAINT fk_customer_email_attachments_email_id FOREIGN KEY (email_id) REFERENCES customer_zoho_emails (id) ON DELETE CASCADE,
    INDEX ix_customer_email_attachments_email_id (email_id)
);

CREATE TABLE IF NOT EXISTS zoho_email_attachment_imports (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    requested_by VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    cancel_requested TINYINT(1) NOT NULL DEFAULT 0,
    consecutive_failures INT NOT NULL DEFAULT 0,
    continue_automatically TINYINT(1) NOT NULL DEFAULT 1,
    requested_limit INT NOT NULL,
    total_attachments INT NOT NULL DEFAULT 0,
    processed_attachments INT NOT NULL DEFAULT 0,
    stored_attachments INT NOT NULL DEFAULT 0,
    failed_attachments INT NOT NULL DEFAULT 0,
    stored_bytes INT NOT NULL DEFAULT 0,
    started_at DATETIME NULL,
    completed_at DATETIME NULL,
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_zoho_email_attachment_imports_status (status)
);

CREATE TABLE IF NOT EXISTS zoho_email_attachment_import_items (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    attachment_import_id INT NOT NULL,
    email_id INT NOT NULL,
    source_attachment_id VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_zoho_email_attachment_import_items_import_attachment UNIQUE (attachment_import_id, email_id, source_attachment_id),
    CONSTRAINT fk_zoho_email_attachment_import_items_import_id FOREIGN KEY (attachment_import_id) REFERENCES zoho_email_attachment_imports (id) ON DELETE CASCADE,
    CONSTRAINT fk_zoho_email_attachment_import_items_email_id FOREIGN KEY (email_id) REFERENCES customer_zoho_emails (id) ON DELETE CASCADE,
    INDEX ix_zoho_email_attachment_import_items_import_id (attachment_import_id),
    INDEX ix_zoho_email_attachment_import_items_email_id (email_id),
    INDEX ix_zoho_email_attachment_import_items_status (status)
);

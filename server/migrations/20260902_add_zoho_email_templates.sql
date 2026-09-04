CREATE TABLE IF NOT EXISTS zoho_email_templates (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    zoho_template_id VARCHAR(255) NOT NULL,
    module VARCHAR(64) NOT NULL DEFAULT 'Accounts',
    encrypted_payload_json TEXT NOT NULL,
    zoho_modified_at DATETIME NULL,
    zoho_synced_at DATETIME NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_zoho_email_templates_zoho_template_id UNIQUE (zoho_template_id),
    INDEX ix_zoho_email_templates_is_active (is_active)
);

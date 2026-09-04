CREATE TABLE IF NOT EXISTS customer_contacts (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    zoho_id VARCHAR(255) NOT NULL,
    encrypted_profile_json TEXT NOT NULL,
    zoho_modified_at DATETIME NULL,
    zoho_synced_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_customer_contacts_customer_id FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE,
    CONSTRAINT uq_customer_contacts_zoho_id UNIQUE (zoho_id),
    INDEX ix_customer_contacts_customer_id (customer_id)
);

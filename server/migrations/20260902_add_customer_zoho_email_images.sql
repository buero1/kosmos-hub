CREATE TABLE IF NOT EXISTS customer_zoho_email_images (
    id INT NOT NULL AUTO_INCREMENT,
    email_id INT NOT NULL,
    source_url_hash VARCHAR(64) NOT NULL,
    encrypted_image_bytes LONGBLOB NOT NULL,
    content_type VARCHAR(128) NOT NULL,
    byte_size INT NOT NULL,
    expires_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT fk_customer_zoho_email_images_email_id FOREIGN KEY (email_id) REFERENCES customer_zoho_emails (id) ON DELETE CASCADE,
    CONSTRAINT uq_customer_zoho_email_images_email_url UNIQUE (email_id, source_url_hash),
    INDEX ix_customer_zoho_email_images_email_id (email_id),
    INDEX ix_customer_zoho_email_images_expires_at (expires_at)
);

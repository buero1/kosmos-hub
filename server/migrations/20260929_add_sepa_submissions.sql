CREATE TABLE IF NOT EXISTS hub_sepa_submissions (
    token_digest VARCHAR(64) NOT NULL PRIMARY KEY,
    customer_id INT NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    received_at DATETIME(6) NULL,
    revoked_at DATETIME(6) NULL,
    payload_digest VARCHAR(64) NULL,
    INDEX ix_hub_sepa_submissions_customer_id (customer_id),
    FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
);

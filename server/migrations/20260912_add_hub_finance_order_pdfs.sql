CREATE TABLE IF NOT EXISTS hub_finance_order_pdfs (
    id INTEGER NOT NULL AUTO_INCREMENT,
    order_id INTEGER NOT NULL,
    source VARCHAR(64) NOT NULL DEFAULT 'zoho-books',
    filename VARCHAR(255) NOT NULL,
    content_type VARCHAR(128) NOT NULL DEFAULT 'application/pdf',
    byte_size INTEGER NOT NULL,
    storage_key VARCHAR(255) NOT NULL,
    imported_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_hub_finance_order_pdfs_order_id (order_id),
    UNIQUE KEY uq_hub_finance_order_pdfs_storage_key (storage_key),
    CONSTRAINT fk_hub_finance_order_pdfs_order_id
        FOREIGN KEY (order_id) REFERENCES hub_finance_orders (id) ON DELETE CASCADE
);

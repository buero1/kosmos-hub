ALTER TABLE customers
    ADD COLUMN zoho_status VARCHAR(255) NULL,
    ADD COLUMN is_visible TINYINT(1) NOT NULL DEFAULT 1;

CREATE INDEX ix_customers_zoho_status ON customers (zoho_status);
CREATE INDEX ix_customers_is_visible ON customers (is_visible);

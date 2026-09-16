CREATE TABLE hub_invoice_email_batches (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  review_nonce VARCHAR(64) NOT NULL UNIQUE,
  actor VARCHAR(64) NOT NULL,
  sender_email VARCHAR(255) NOT NULL,
  template_id VARCHAR(255) NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'queued',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE hub_invoice_email_batch_items (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  batch_id INT NOT NULL,
  invoice_id INT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'queued',
  encrypted_payload_json MEDIUMTEXT NOT NULL,
  error TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_hub_invoice_email_batch_item (batch_id, invoice_id),
  INDEX ix_hub_invoice_email_batch_items_batch_id (batch_id),
  INDEX ix_hub_invoice_email_batch_items_invoice_id (invoice_id),
  CONSTRAINT fk_invoice_email_batch_item_batch FOREIGN KEY (batch_id) REFERENCES hub_invoice_email_batches (id) ON DELETE CASCADE,
  CONSTRAINT fk_invoice_email_batch_item_invoice FOREIGN KEY (invoice_id) REFERENCES hub_finance_invoices (id) ON DELETE SET NULL
);

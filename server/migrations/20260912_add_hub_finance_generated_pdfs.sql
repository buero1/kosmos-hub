ALTER TABLE hub_finance_offers
  ADD COLUMN pdf_template_id INT NULL AFTER contact_id,
  ADD INDEX ix_hub_finance_offers_pdf_template_id (pdf_template_id),
  ADD CONSTRAINT fk_hub_finance_offers_pdf_template_id_hub_pdf_templates
    FOREIGN KEY (pdf_template_id) REFERENCES hub_pdf_templates (id) ON DELETE SET NULL;

ALTER TABLE hub_finance_orders
  ADD COLUMN pdf_template_id INT NULL AFTER offer_id,
  ADD INDEX ix_hub_finance_orders_pdf_template_id (pdf_template_id),
  ADD CONSTRAINT fk_hub_finance_orders_pdf_template_id_hub_pdf_templates
    FOREIGN KEY (pdf_template_id) REFERENCES hub_pdf_templates (id) ON DELETE SET NULL;

ALTER TABLE hub_finance_invoices
  ADD COLUMN pdf_template_id INT NULL AFTER order_id,
  ADD INDEX ix_hub_finance_invoices_pdf_template_id (pdf_template_id),
  ADD CONSTRAINT fk_hub_finance_invoices_pdf_template_id_hub_pdf_templates
    FOREIGN KEY (pdf_template_id) REFERENCES hub_pdf_templates (id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS hub_finance_generated_pdfs (
  id INT NOT NULL AUTO_INCREMENT,
  document_type VARCHAR(32) NOT NULL,
  document_id INT NOT NULL,
  template_id INT NULL,
  template_revision_id INT NULL,
  template_name VARCHAR(255) NOT NULL,
  template_version INT NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'queued',
  generation_token VARCHAR(64) NOT NULL,
  filename VARCHAR(255) NULL,
  content_type VARCHAR(128) NOT NULL DEFAULT 'application/pdf',
  byte_size INT NULL,
  storage_key VARCHAR(255) NULL,
  is_zugferd BOOLEAN NOT NULL DEFAULT FALSE,
  zugferd_version VARCHAR(32) NULL,
  zugferd_profile VARCHAR(32) NULL,
  validation_status VARCHAR(32) NOT NULL DEFAULT 'not-applicable',
  error_message LONGTEXT NULL,
  requested_at DATETIME NOT NULL,
  generation_started_at DATETIME NULL,
  generated_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  CONSTRAINT fk_hub_finance_generated_pdfs_template_id_hub_pdf_templates
    FOREIGN KEY (template_id) REFERENCES hub_pdf_templates (id) ON DELETE SET NULL,
  CONSTRAINT fk_hub_finance_generated_pdfs_template_revision_id_hub_pdf_template_revisions
    FOREIGN KEY (template_revision_id) REFERENCES hub_pdf_template_revisions (id) ON DELETE SET NULL,
  CONSTRAINT uq_hub_finance_generated_pdfs_document UNIQUE (document_type, document_id),
  CONSTRAINT uq_hub_finance_generated_pdfs_generation_token UNIQUE (generation_token),
  CONSTRAINT uq_hub_finance_generated_pdfs_storage_key UNIQUE (storage_key),
  INDEX ix_hub_finance_generated_pdfs_document_type (document_type),
  INDEX ix_hub_finance_generated_pdfs_document_id (document_id),
  INDEX ix_hub_finance_generated_pdfs_template_id (template_id),
  INDEX ix_hub_finance_generated_pdfs_template_revision_id (template_revision_id),
  INDEX ix_hub_finance_generated_pdfs_status (status)
);

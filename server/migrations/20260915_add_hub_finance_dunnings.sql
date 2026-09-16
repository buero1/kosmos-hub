CREATE TABLE hub_finance_dunnings (
  id INT NOT NULL AUTO_INCREMENT,
  dunning_number VARCHAR(255) NULL,
  customer_id INT NULL,
  contact_id INT NULL,
  invoice_id INT NULL,
  pdf_template_id INT NULL,
  encrypted_fields_json TEXT NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY ix_hub_finance_dunnings_dunning_number (dunning_number),
  KEY ix_hub_finance_dunnings_customer_id (customer_id),
  KEY ix_hub_finance_dunnings_contact_id (contact_id),
  KEY ix_hub_finance_dunnings_invoice_id (invoice_id),
  KEY ix_hub_finance_dunnings_pdf_template_id (pdf_template_id),
  CONSTRAINT fk_hub_finance_dunnings_customer FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE SET NULL,
  CONSTRAINT fk_hub_finance_dunnings_contact FOREIGN KEY (contact_id) REFERENCES customer_contacts (id) ON DELETE SET NULL,
  CONSTRAINT fk_hub_finance_dunnings_invoice FOREIGN KEY (invoice_id) REFERENCES hub_finance_invoices (id) ON DELETE SET NULL,
  CONSTRAINT fk_hub_finance_dunnings_pdf_template FOREIGN KEY (pdf_template_id) REFERENCES hub_pdf_templates (id) ON DELETE SET NULL
);

CREATE TABLE hub_finance_dunning_lines (
  id INT NOT NULL AUTO_INCREMENT,
  dunning_id INT NOT NULL,
  article_id INT NULL,
  position_index INT NOT NULL,
  encrypted_fields_json TEXT NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  KEY ix_hub_finance_dunning_lines_dunning_id (dunning_id),
  KEY ix_hub_finance_dunning_lines_article_id (article_id),
  CONSTRAINT fk_hub_finance_dunning_lines_dunning FOREIGN KEY (dunning_id) REFERENCES hub_finance_dunnings (id) ON DELETE CASCADE,
  CONSTRAINT fk_hub_finance_dunning_lines_article FOREIGN KEY (article_id) REFERENCES hub_finance_articles (id) ON DELETE SET NULL
);

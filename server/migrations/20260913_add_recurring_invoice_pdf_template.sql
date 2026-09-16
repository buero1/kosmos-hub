ALTER TABLE hub_finance_recurring_invoices
  ADD COLUMN pdf_template_id INT NULL AFTER contact_id,
  ADD INDEX ix_hub_finance_recurring_invoices_pdf_template_id (pdf_template_id),
  ADD CONSTRAINT fk_recurring_invoices_pdf_template
    FOREIGN KEY (pdf_template_id) REFERENCES hub_pdf_templates (id) ON DELETE SET NULL;

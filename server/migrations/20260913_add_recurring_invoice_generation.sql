ALTER TABLE hub_finance_recurring_invoices
  ADD COLUMN hub_next_run_on DATE NULL,
  ADD INDEX ix_hub_finance_recurring_invoices_hub_next_run_on (hub_next_run_on);

ALTER TABLE hub_finance_invoices
  ADD COLUMN recurring_invoice_id INT NULL,
  ADD COLUMN recurring_scheduled_on DATE NULL,
  ADD INDEX ix_hub_finance_invoices_recurring_invoice_id (recurring_invoice_id),
  ADD CONSTRAINT uq_hub_finance_invoices_recurring_occurrence
    UNIQUE (recurring_invoice_id, recurring_scheduled_on),
  ADD CONSTRAINT fk_hub_finance_invoices_recurring_invoice
    FOREIGN KEY (recurring_invoice_id) REFERENCES hub_finance_recurring_invoices (id) ON DELETE SET NULL;

ALTER TABLE hub_finance_generated_pdfs
  ADD COLUMN attempt_count INT NOT NULL DEFAULT 0,
  ADD COLUMN next_retry_at DATETIME NULL;

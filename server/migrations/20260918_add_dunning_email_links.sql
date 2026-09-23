ALTER TABLE customer_zoho_emails
  ADD COLUMN dunning_id INT NULL AFTER customer_id,
  ADD INDEX ix_customer_zoho_emails_dunning_id (dunning_id),
  ADD CONSTRAINT fk_customer_zoho_emails_dunning
    FOREIGN KEY (dunning_id) REFERENCES hub_finance_dunnings (id) ON DELETE SET NULL;

ALTER TABLE hub_scheduled_emails
  ADD COLUMN dunning_id INT NULL AFTER customer_id,
  ADD INDEX ix_hub_scheduled_emails_dunning_id (dunning_id),
  ADD CONSTRAINT fk_hub_scheduled_emails_dunning
    FOREIGN KEY (dunning_id) REFERENCES hub_finance_dunnings (id) ON DELETE SET NULL;

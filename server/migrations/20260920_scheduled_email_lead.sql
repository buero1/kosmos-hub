ALTER TABLE hub_scheduled_emails
  ADD COLUMN lead_id INT NULL,
  ADD INDEX ix_hub_scheduled_emails_lead_id (lead_id),
  ADD CONSTRAINT fk_hub_scheduled_emails_lead FOREIGN KEY (lead_id)
    REFERENCES hub_leads (id) ON DELETE SET NULL;

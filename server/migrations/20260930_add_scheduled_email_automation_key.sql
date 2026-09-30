ALTER TABLE hub_scheduled_emails
  ADD COLUMN automation_key VARCHAR(255) NULL AFTER message_id,
  ADD UNIQUE INDEX uq_hub_scheduled_emails_automation_key (automation_key);

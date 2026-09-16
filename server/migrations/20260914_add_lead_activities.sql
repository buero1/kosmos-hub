ALTER TABLE customer_call_activities
  ADD COLUMN lead_id INT NULL,
  ADD INDEX ix_customer_call_activities_lead_id (lead_id),
  ADD INDEX ix_customer_call_activities_lead_starts_at (lead_id, starts_at),
  ADD CONSTRAINT fk_customer_call_activities_lead_id FOREIGN KEY (lead_id) REFERENCES hub_leads (id) ON DELETE CASCADE;

ALTER TABLE customer_task_activities
  ADD COLUMN lead_id INT NULL,
  ADD INDEX ix_customer_task_activities_lead_id (lead_id),
  ADD INDEX ix_customer_task_activities_lead_due_at (lead_id, due_at),
  ADD CONSTRAINT fk_customer_task_activities_lead_id FOREIGN KEY (lead_id) REFERENCES hub_leads (id) ON DELETE CASCADE;

ALTER TABLE customer_meeting_activities
  ADD COLUMN lead_id INT NULL,
  ADD INDEX ix_customer_meeting_activities_lead_id (lead_id),
  ADD INDEX ix_customer_meeting_activities_lead_starts_at (lead_id, starts_at),
  ADD CONSTRAINT fk_customer_meeting_activities_lead_id FOREIGN KEY (lead_id) REFERENCES hub_leads (id) ON DELETE CASCADE;

ALTER TABLE hub_finance_offers
  ADD COLUMN lead_id INT NULL AFTER customer_id,
  ADD INDEX ix_hub_finance_offers_lead_id (lead_id),
  ADD CONSTRAINT fk_hub_finance_offers_lead
    FOREIGN KEY (lead_id) REFERENCES hub_leads (id) ON DELETE SET NULL;

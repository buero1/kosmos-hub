ALTER TABLE hub_finance_offers
  ADD COLUMN unassigned_owner_user_id INT NULL,
  ADD INDEX ix_hub_finance_offers_unassigned_owner_user_id (unassigned_owner_user_id),
  ADD CONSTRAINT fk_hub_finance_offers_unassigned_owner
    FOREIGN KEY (unassigned_owner_user_id) REFERENCES hub_users (id) ON DELETE SET NULL;

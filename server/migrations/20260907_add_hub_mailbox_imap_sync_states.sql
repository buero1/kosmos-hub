CREATE TABLE hub_mailbox_imap_sync_states (
  id INT NOT NULL AUTO_INCREMENT,
  mailbox_account_id INT NOT NULL,
  folder VARCHAR(128) NOT NULL,
  last_imap_uid VARCHAR(32) NULL,
  last_synced_at DATETIME NULL,
  last_error TEXT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT uq_hub_mailbox_imap_sync_state_folder UNIQUE (mailbox_account_id, folder),
  CONSTRAINT fk_hub_mailbox_imap_sync_states_account FOREIGN KEY (mailbox_account_id) REFERENCES hub_mailbox_accounts(id) ON DELETE CASCADE,
  INDEX ix_hub_mailbox_imap_sync_states_last_synced_at (last_synced_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

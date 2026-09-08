CREATE TABLE hub_mailbox_imap_imports (
  id INT NOT NULL AUTO_INCREMENT,
  requested_by VARCHAR(128) NOT NULL,
  since_date DATE NOT NULL,
  status VARCHAR(32) NOT NULL,
  cancel_requested TINYINT(1) NOT NULL DEFAULT 0,
  consecutive_failures INT NOT NULL DEFAULT 0,
  total_messages INT NOT NULL DEFAULT 0,
  processed_messages INT NOT NULL DEFAULT 0,
  imported_messages INT NOT NULL DEFAULT 0,
  skipped_messages INT NOT NULL DEFAULT 0,
  failed_messages INT NOT NULL DEFAULT 0,
  stored_attachments INT NOT NULL DEFAULT 0,
  stored_bytes INT NOT NULL DEFAULT 0,
  started_at DATETIME NULL,
  completed_at DATETIME NULL,
  last_error TEXT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  INDEX ix_hub_mailbox_imap_imports_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE hub_mailbox_imap_import_items (
  id INT NOT NULL AUTO_INCREMENT,
  mailbox_import_id INT NOT NULL,
  mailbox_account_id INT NOT NULL,
  folder VARCHAR(128) NOT NULL,
  imap_uid VARCHAR(32) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  last_error TEXT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT uq_hub_mailbox_imap_import_item UNIQUE (mailbox_import_id, mailbox_account_id, folder, imap_uid),
  CONSTRAINT fk_hub_mailbox_imap_import_items_import FOREIGN KEY (mailbox_import_id) REFERENCES hub_mailbox_imap_imports(id) ON DELETE CASCADE,
  CONSTRAINT fk_hub_mailbox_imap_import_items_account FOREIGN KEY (mailbox_account_id) REFERENCES hub_mailbox_accounts(id) ON DELETE CASCADE,
  INDEX ix_hub_mailbox_imap_import_items_status (mailbox_import_id, status, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE hub_mailbox_attachments (
  id INT NOT NULL AUTO_INCREMENT,
  email_id INT NOT NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'mittwald-imap',
  source_attachment_id VARCHAR(255) NOT NULL,
  storage_key VARCHAR(96) NOT NULL,
  content_type VARCHAR(128) NOT NULL DEFAULT 'application/octet-stream',
  byte_size INT NOT NULL,
  stored_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT uq_hub_mailbox_attachments_email_source UNIQUE (email_id, source_attachment_id),
  CONSTRAINT uq_hub_mailbox_attachments_storage_key UNIQUE (storage_key),
  CONSTRAINT fk_hub_mailbox_attachments_email FOREIGN KEY (email_id) REFERENCES hub_mailbox_emails(id) ON DELETE CASCADE,
  INDEX ix_hub_mailbox_attachments_email_id (email_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE hub_integration_tokens (
  id INT NOT NULL AUTO_INCREMENT,
  user_id INT NOT NULL,
  name VARCHAR(80) NOT NULL,
  source_key VARCHAR(48) NOT NULL DEFAULT 'callapp',
  token_prefix VARCHAR(24) NOT NULL,
  token_digest VARCHAR(64) NOT NULL,
  last_used_at DATETIME NULL,
  revoked_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_hub_integration_tokens_user_name (user_id, name),
  UNIQUE KEY uq_hub_integration_tokens_token_digest (token_digest),
  KEY ix_hub_integration_tokens_user_id (user_id),
  CONSTRAINT fk_hub_integration_tokens_user_id_hub_users FOREIGN KEY (user_id) REFERENCES hub_users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE hub_leads
  ADD COLUMN source_system VARCHAR(96) NULL,
  ADD COLUMN source_external_id VARCHAR(255) NULL,
  ADD UNIQUE KEY uq_hub_leads_source_external (source_system, source_external_id);

ALTER TABLE hub_lead_notes
  ADD COLUMN source_system VARCHAR(96) NULL,
  ADD COLUMN source_external_id VARCHAR(255) NULL,
  ADD UNIQUE KEY uq_hub_lead_notes_source_external (lead_id, source_system, source_external_id);

ALTER TABLE customer_call_activities
  ADD COLUMN source_system VARCHAR(96) NULL,
  ADD COLUMN source_external_id VARCHAR(255) NULL,
  ADD COLUMN duration_seconds INT NULL,
  ADD COLUMN recording_url TEXT NULL,
  ADD COLUMN transcript_url TEXT NULL,
  ADD UNIQUE KEY uq_customer_call_activities_source_external (source_system, source_external_id);

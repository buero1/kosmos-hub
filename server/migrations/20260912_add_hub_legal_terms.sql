CREATE TABLE IF NOT EXISTS hub_legal_terms (
  id INT NOT NULL AUTO_INCREMENT,
  name VARCHAR(255) NOT NULL,
  version INT NOT NULL DEFAULT 1,
  content_html LONGTEXT NOT NULL,
  is_archived BOOLEAN NOT NULL DEFAULT FALSE,
  created_by_username VARCHAR(255) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX ix_hub_legal_terms_is_archived (is_archived)
);

CREATE TABLE IF NOT EXISTS hub_legal_terms_revisions (
  id INT NOT NULL AUTO_INCREMENT,
  legal_terms_id INT NOT NULL,
  version INT NOT NULL,
  name VARCHAR(255) NOT NULL,
  content_html LONGTEXT NOT NULL,
  created_by_username VARCHAR(255) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  CONSTRAINT fk_hub_legal_terms_revisions_legal_terms_id_hub_legal_terms
    FOREIGN KEY (legal_terms_id) REFERENCES hub_legal_terms (id) ON DELETE CASCADE,
  CONSTRAINT uq_hub_legal_terms_revisions_version UNIQUE (legal_terms_id, version),
  INDEX ix_hub_legal_terms_revisions_legal_terms_id (legal_terms_id)
);

ALTER TABLE hub_pdf_templates
  ADD COLUMN legal_terms_id INT NULL AFTER content_json,
  ADD INDEX ix_hub_pdf_templates_legal_terms_id (legal_terms_id),
  ADD CONSTRAINT fk_hub_pdf_templates_legal_terms_id_hub_legal_terms
    FOREIGN KEY (legal_terms_id) REFERENCES hub_legal_terms (id) ON DELETE SET NULL;

ALTER TABLE hub_pdf_template_revisions
  ADD COLUMN legal_terms_revision_id INT NULL AFTER content_json,
  ADD INDEX ix_hub_pdf_template_revisions_legal_terms_revision_id (legal_terms_revision_id),
  ADD CONSTRAINT fk_hub_pdf_template_revisions_legal_terms_revision_id
    FOREIGN KEY (legal_terms_revision_id) REFERENCES hub_legal_terms_revisions (id) ON DELETE SET NULL;

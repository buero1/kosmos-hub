CREATE TABLE IF NOT EXISTS hub_pdf_templates (
  id INT NOT NULL AUTO_INCREMENT,
  document_type VARCHAR(32) NOT NULL,
  name VARCHAR(255) NOT NULL,
  is_default BOOLEAN NOT NULL DEFAULT FALSE,
  version INT NOT NULL DEFAULT 1,
  content_json LONGTEXT NOT NULL,
  created_by_username VARCHAR(255) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX ix_hub_pdf_templates_document_type (document_type),
  INDEX ix_hub_pdf_templates_is_default (is_default)
);

CREATE TABLE IF NOT EXISTS hub_pdf_template_revisions (
  id INT NOT NULL AUTO_INCREMENT,
  template_id INT NOT NULL,
  version INT NOT NULL,
  name VARCHAR(255) NOT NULL,
  content_json LONGTEXT NOT NULL,
  created_by_username VARCHAR(255) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  CONSTRAINT fk_hub_pdf_template_revisions_template_id_hub_pdf_templates
    FOREIGN KEY (template_id) REFERENCES hub_pdf_templates (id) ON DELETE CASCADE,
  CONSTRAINT uq_hub_pdf_template_revisions_template_version UNIQUE (template_id, version),
  INDEX ix_hub_pdf_template_revisions_template_id (template_id)
);

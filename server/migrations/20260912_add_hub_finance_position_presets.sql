CREATE TABLE IF NOT EXISTS hub_finance_position_presets (
  id INT NOT NULL AUTO_INCREMENT,
  library_key VARCHAR(32) NOT NULL,
  name VARCHAR(255) NOT NULL,
  normalized_name VARCHAR(255) NOT NULL,
  encrypted_lines_json LONGTEXT NOT NULL,
  created_by_username VARCHAR(255) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  CONSTRAINT uq_hub_finance_position_presets_library_name UNIQUE (library_key, normalized_name),
  INDEX ix_hub_finance_position_presets_library_key (library_key)
);

CREATE TABLE IF NOT EXISTS hub_teams (
  id INT NOT NULL AUTO_INCREMENT,
  name VARCHAR(96) NOT NULL,
  description VARCHAR(500) NOT NULL DEFAULT '',
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_hub_teams_name (name)
);

CREATE TABLE IF NOT EXISTS hub_access_roles (
  `key` VARCHAR(64) NOT NULL,
  name VARCHAR(96) NOT NULL,
  description VARCHAR(500) NOT NULL DEFAULT '',
  is_system BOOLEAN NOT NULL DEFAULT FALSE,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`key`),
  UNIQUE KEY uq_hub_access_roles_name (name)
);

CREATE TABLE IF NOT EXISTS hub_role_permissions (
  id INT NOT NULL AUTO_INCREMENT,
  role_key VARCHAR(64) NOT NULL,
  module_key VARCHAR(48) NOT NULL,
  can_view BOOLEAN NOT NULL DEFAULT FALSE,
  can_create BOOLEAN NOT NULL DEFAULT FALSE,
  can_edit BOOLEAN NOT NULL DEFAULT FALSE,
  can_delete BOOLEAN NOT NULL DEFAULT FALSE,
  can_export BOOLEAN NOT NULL DEFAULT FALSE,
  can_manage BOOLEAN NOT NULL DEFAULT FALSE,
  record_scope VARCHAR(16) NOT NULL DEFAULT 'none',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_hub_role_permissions_role_module (role_key, module_key),
  INDEX ix_hub_role_permissions_role_key (role_key),
  INDEX ix_hub_role_permissions_module_key (module_key),
  CONSTRAINT fk_hub_role_permissions_role FOREIGN KEY (role_key) REFERENCES hub_access_roles (`key`) ON DELETE CASCADE
);

ALTER TABLE hub_users MODIFY COLUMN role VARCHAR(64) NOT NULL;
ALTER TABLE hub_users ADD COLUMN team_id INT NULL;
CREATE INDEX ix_hub_users_team_id ON hub_users (team_id);
ALTER TABLE hub_users ADD CONSTRAINT fk_hub_users_team_id_hub_teams FOREIGN KEY (team_id) REFERENCES hub_teams (id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS hub_record_assignments (
  id INT NOT NULL AUTO_INCREMENT,
  module_key VARCHAR(48) NOT NULL,
  record_id INT NOT NULL,
  owner_user_id INT NULL,
  team_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_hub_record_assignments_module_record (module_key, record_id),
  INDEX ix_hub_record_assignments_record_id (record_id),
  INDEX ix_hub_record_assignments_owner (module_key, owner_user_id),
  INDEX ix_hub_record_assignments_team (module_key, team_id),
  CONSTRAINT fk_hub_record_assignments_owner FOREIGN KEY (owner_user_id) REFERENCES hub_users (id) ON DELETE SET NULL,
  CONSTRAINT fk_hub_record_assignments_team_id FOREIGN KEY (team_id) REFERENCES hub_teams (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS hub_record_access_grants (
  id INT NOT NULL AUTO_INCREMENT,
  module_key VARCHAR(48) NOT NULL,
  record_id INT NOT NULL,
  user_id INT NULL,
  team_id INT NULL,
  can_edit BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX ix_hub_record_access_grants_record_id (record_id),
  INDEX ix_hub_record_access_grants_record (module_key, record_id),
  INDEX ix_hub_record_access_grants_user (module_key, user_id),
  INDEX ix_hub_record_access_grants_team (module_key, team_id),
  CONSTRAINT ck_hub_record_access_grants_target_exactly_one CHECK ((user_id IS NOT NULL AND team_id IS NULL) OR (user_id IS NULL AND team_id IS NOT NULL)),
  CONSTRAINT fk_hub_record_access_grants_user_id_hub_users FOREIGN KEY (user_id) REFERENCES hub_users (id) ON DELETE CASCADE,
  CONSTRAINT fk_hub_record_access_grants_team_id_hub_teams FOREIGN KEY (team_id) REFERENCES hub_teams (id) ON DELETE CASCADE
);

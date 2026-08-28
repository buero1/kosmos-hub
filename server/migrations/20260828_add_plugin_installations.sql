CREATE TABLE IF NOT EXISTS plugin_installation_packages (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    original_filename VARCHAR(255) NOT NULL,
    plugin_file VARCHAR(255) NOT NULL,
    plugin_name VARCHAR(255) NOT NULL,
    plugin_version VARCHAR(128) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    package_bytes LONGBLOB NOT NULL,
    expires_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_plugin_installation_packages_plugin_file (plugin_file),
    INDEX ix_plugin_installation_packages_sha256 (sha256),
    INDEX ix_plugin_installation_packages_expires_at (expires_at)
);

ALTER TABLE maintenance_runs ADD COLUMN plugin_installation_package_id INT NULL;

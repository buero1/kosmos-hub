CREATE TABLE IF NOT EXISTS fleet_refresh_site_results (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    fleet_refresh_run_id INT NOT NULL,
    site_id INT NOT NULL,
    domain VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    state_status VARCHAR(32) NOT NULL,
    updates_status VARCHAR(32) NOT NULL,
    backups_status VARCHAR(32) NOT NULL,
    users_status VARCHAR(32) NOT NULL,
    jet_status VARCHAR(64) NOT NULL DEFAULT 'not-applicable',
    completed_at DATETIME NULL,
    detail TEXT NULL,
    result_json JSON NOT NULL,
    error_message TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_fleet_refresh_site_results_fleet_refresh_run_id (fleet_refresh_run_id, site_id),
    INDEX ix_fleet_refresh_site_results_fleet_refresh_run_id (fleet_refresh_run_id),
    INDEX ix_fleet_refresh_site_results_site_id (site_id),
    INDEX ix_fleet_refresh_site_results_status (status),
    CONSTRAINT fk_fleet_refresh_site_results_fleet_refresh_run_id
        FOREIGN KEY (fleet_refresh_run_id) REFERENCES fleet_refresh_runs(id),
    CONSTRAINT fk_fleet_refresh_site_results_site_id
        FOREIGN KEY (site_id) REFERENCES sites(id)
);

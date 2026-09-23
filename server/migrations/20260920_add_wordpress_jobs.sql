CREATE TABLE IF NOT EXISTS hub_wordpress_jobs (
    id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
    operation_key VARCHAR(128) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    site_ids JSON NOT NULL,
    encrypted_input LONGTEXT NULL,
    result_json JSON NOT NULL,
    message TEXT NOT NULL,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_hub_wordpress_jobs_actor (actor),
    INDEX ix_hub_wordpress_jobs_status (status)
);

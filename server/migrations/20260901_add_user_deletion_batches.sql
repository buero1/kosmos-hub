CREATE TABLE IF NOT EXISTS user_deletion_batches (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    requested_by VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    cancellation_requested TINYINT(1) NOT NULL DEFAULT 0,
    started_at DATETIME NULL,
    completed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_user_deletion_batches_status (status)
);

CREATE TABLE IF NOT EXISTS user_deletion_batch_items (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_deletion_batch_id INT NOT NULL,
    site_id INT NOT NULL,
    position INT NOT NULL,
    target_user_id INT NOT NULL,
    target_username VARCHAR(255) NOT NULL,
    replacement_user_id INT NULL,
    replacement_username VARCHAR(255) NULL,
    status VARCHAR(32) NOT NULL,
    message TEXT NULL,
    started_at DATETIME NULL,
    completed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_deletion_batch_items_user_deletion_batch_id
        FOREIGN KEY (user_deletion_batch_id) REFERENCES user_deletion_batches (id) ON DELETE CASCADE,
    CONSTRAINT fk_user_deletion_batch_items_site_id
        FOREIGN KEY (site_id) REFERENCES sites (id),
    INDEX ix_user_deletion_batch_items_user_deletion_batch_id (user_deletion_batch_id),
    INDEX ix_user_deletion_batch_items_site_id (site_id),
    INDEX ix_user_deletion_batch_items_status (status)
);

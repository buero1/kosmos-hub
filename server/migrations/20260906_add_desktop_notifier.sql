CREATE TABLE IF NOT EXISTS hub_desktop_devices (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    name VARCHAR(80) NOT NULL,
    token_prefix VARCHAR(24) NOT NULL,
    token_digest VARCHAR(64) NOT NULL,
    last_seen_at DATETIME NULL,
    revoked_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_hub_desktop_devices_user_name UNIQUE (user_id, name),
    CONSTRAINT uq_hub_desktop_devices_token_digest UNIQUE (token_digest),
    CONSTRAINT fk_hub_desktop_devices_user_id FOREIGN KEY (user_id) REFERENCES hub_users (id),
    INDEX ix_hub_desktop_devices_user_id (user_id),
    INDEX ix_hub_desktop_devices_token_digest (token_digest)
);

CREATE TABLE IF NOT EXISTS customer_activity_reminder_notifications (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    customer_id INT NOT NULL,
    activity_kind VARCHAR(16) NOT NULL,
    activity_id INT NOT NULL,
    reminder_key VARCHAR(32) NOT NULL,
    remind_at DATETIME NOT NULL,
    snoozed_until DATETIME NULL,
    completed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_customer_activity_reminder_notifications_source UNIQUE (user_id, activity_kind, activity_id, reminder_key),
    CONSTRAINT fk_customer_activity_reminder_notifications_user_id FOREIGN KEY (user_id) REFERENCES hub_users (id) ON DELETE CASCADE,
    CONSTRAINT fk_customer_activity_reminder_notifications_customer_id FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE,
    INDEX ix_customer_activity_reminder_notifications_user_id (user_id),
    INDEX ix_customer_activity_reminder_notifications_customer_id (customer_id),
    INDEX ix_customer_activity_reminder_notifications_due (user_id, completed_at, snoozed_until, remind_at)
);

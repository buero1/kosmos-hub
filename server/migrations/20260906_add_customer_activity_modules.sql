CREATE TABLE IF NOT EXISTS customer_call_activities (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'planned',
    direction VARCHAR(32) NOT NULL DEFAULT 'outbound',
    starts_at DATETIME NOT NULL,
    ends_at DATETIME NOT NULL,
    duration_minutes INT NOT NULL DEFAULT 30,
    reminder_channel VARCHAR(32) NULL,
    reminder_minutes_before INT NULL,
    description TEXT NULL,
    created_by_username VARCHAR(64) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_customer_call_activities_customer_id FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE,
    INDEX ix_customer_call_activities_customer_id (customer_id),
    INDEX ix_customer_call_activities_customer_starts_at (customer_id, starts_at)
);

CREATE TABLE IF NOT EXISTS customer_call_reminders (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    call_id INT NOT NULL,
    channel VARCHAR(16) NOT NULL,
    minutes_before INT NOT NULL,
    sort_order INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_customer_call_reminders_call_id FOREIGN KEY (call_id) REFERENCES customer_call_activities (id) ON DELETE CASCADE,
    INDEX ix_customer_call_reminders_call_id (call_id),
    INDEX ix_customer_call_reminders_call_sort_order (call_id, sort_order)
);

CREATE TABLE IF NOT EXISTS customer_task_activities (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'planned',
    due_at DATETIME NULL,
    reminder_channel VARCHAR(16) NULL,
    reminder_minutes_before INT NULL,
    description TEXT NULL,
    created_by_username VARCHAR(64) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_customer_task_activities_customer_id FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE,
    INDEX ix_customer_task_activities_customer_id (customer_id),
    INDEX ix_customer_task_activities_customer_due_at (customer_id, due_at)
);

CREATE TABLE IF NOT EXISTS customer_meeting_activities (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'planned',
    starts_at DATETIME NULL,
    ends_at DATETIME NULL,
    description TEXT NULL,
    created_by_username VARCHAR(64) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_customer_meeting_activities_customer_id FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE,
    INDEX ix_customer_meeting_activities_customer_id (customer_id),
    INDEX ix_customer_meeting_activities_customer_starts_at (customer_id, starts_at)
);

CREATE TABLE IF NOT EXISTS customer_meeting_reminders (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    meeting_id INT NOT NULL,
    channel VARCHAR(16) NOT NULL,
    minutes_before INT NOT NULL,
    sort_order INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_customer_meeting_reminders_meeting_id FOREIGN KEY (meeting_id) REFERENCES customer_meeting_activities (id) ON DELETE CASCADE,
    INDEX ix_customer_meeting_reminders_meeting_id (meeting_id),
    INDEX ix_customer_meeting_reminders_meeting_sort_order (meeting_id, sort_order)
);

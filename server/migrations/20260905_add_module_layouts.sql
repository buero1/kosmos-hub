CREATE TABLE IF NOT EXISTS module_layouts (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    layout_key VARCHAR(128) NOT NULL,
    item_order_json TEXT NOT NULL,
    configured_by_user_id INT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_module_layouts_layout_key UNIQUE (layout_key),
    CONSTRAINT fk_module_layouts_configured_by_user_id
        FOREIGN KEY (configured_by_user_id) REFERENCES hub_users (id)
);

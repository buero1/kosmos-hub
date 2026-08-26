ALTER TABLE fleet_refresh_settings
    ADD COLUMN max_parallel_direct_updates INT NOT NULL DEFAULT 5
    AFTER max_parallel_site_checks;

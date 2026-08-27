ALTER TABLE fleet_refresh_settings
  ADD COLUMN auto_refresh_enabled TINYINT(1) NOT NULL DEFAULT 1 AFTER max_parallel_direct_updates,
  ADD COLUMN auto_refresh_interval_hours INT NOT NULL DEFAULT 24 AFTER auto_refresh_enabled,
  ADD COLUMN auto_refresh_time VARCHAR(5) NOT NULL DEFAULT '03:00' AFTER auto_refresh_interval_hours;

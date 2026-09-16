CREATE TABLE IF NOT EXISTS hub_activity_events (
  id INT NOT NULL AUTO_INCREMENT,
  timestamp DATETIME NOT NULL,
  actor VARCHAR(64) NOT NULL,
  module_key VARCHAR(64) NOT NULL,
  resource_id VARCHAR(64) NULL,
  category VARCHAR(24) NOT NULL,
  action VARCHAR(128) NOT NULL,
  result VARCHAR(24) NOT NULL,
  origin VARCHAR(24) NOT NULL,
  changed_fields TEXT NULL,
  PRIMARY KEY (id),
  INDEX ix_hub_activity_module_time (module_key, id),
  INDEX ix_hub_activity_record_time (module_key, resource_id, id),
  INDEX ix_hub_activity_actor_time (actor, id),
  INDEX ix_hub_activity_timestamp (timestamp, id)
);

CREATE TABLE center_license (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  client_id BIGINT NOT NULL,
  granted_days_total BIGINT NOT NULL,
  expires_at DATETIME(3) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
  CONSTRAINT chk_center_license_status CHECK (status IN ('ACTIVE', 'DISABLED')),
  disabled_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY uk_center_license_client (client_id),
  CONSTRAINT fk_center_license_client FOREIGN KEY (client_id) REFERENCES center_client(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE center_license_operation (
  request_id CHAR(36) NOT NULL PRIMARY KEY,
  client_id BIGINT NOT NULL,
  actor_user_id BIGINT NOT NULL,
  action VARCHAR(16) NOT NULL,
  days_added INT NOT NULL DEFAULT 0,
  previous_expires_at DATETIME(3) NULL,
  resulting_expires_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL,
  KEY idx_center_license_operation_client_time (client_id, created_at),
  CONSTRAINT fk_center_license_operation_client FOREIGN KEY (client_id) REFERENCES center_client(id),
  CONSTRAINT fk_center_license_operation_user FOREIGN KEY (actor_user_id) REFERENCES center_user(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE center_heartbeat (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  client_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  received_at DATETIME(3) NOT NULL,
  license_status VARCHAR(16) NOT NULL,
  should_lock BOOLEAN NOT NULL,
  remaining_seconds BIGINT NOT NULL,
  client_sequence BIGINT NULL,
  client_reported_at DATETIME(3) NULL,
  clock_skew_seconds BIGINT NULL,
  clock_anomaly BOOLEAN NOT NULL DEFAULT FALSE,
  offline_since DATETIME(3) NULL,
  KEY idx_center_heartbeat_client_time (client_id, received_at),
  CONSTRAINT fk_center_heartbeat_client FOREIGN KEY (client_id) REFERENCES center_client(id),
  CONSTRAINT fk_center_heartbeat_user FOREIGN KEY (user_id) REFERENCES center_user(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO center_permission (id, permission_key, permission_name) VALUES
  (7, 'license:read', '查看机器授权'),
  (8, 'license:write', '管理机器授权');

INSERT INTO center_role_permission (role_id, permission_id) VALUES
  (1, 7), (1, 8), (2, 7);

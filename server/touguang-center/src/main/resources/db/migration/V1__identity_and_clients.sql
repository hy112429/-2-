CREATE TABLE center_user (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(64) NOT NULL,
  password_hash VARCHAR(100) NOT NULL,
  display_name VARCHAR(100) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_center_user_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE center_role (
  id BIGINT NOT NULL PRIMARY KEY,
  role_key VARCHAR(64) NOT NULL,
  role_name VARCHAR(64) NOT NULL,
  UNIQUE KEY uk_center_role_key (role_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE center_permission (
  id BIGINT NOT NULL PRIMARY KEY,
  permission_key VARCHAR(64) NOT NULL,
  permission_name VARCHAR(64) NOT NULL,
  UNIQUE KEY uk_center_permission_key (permission_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE center_user_role (
  user_id BIGINT NOT NULL,
  role_id BIGINT NOT NULL,
  PRIMARY KEY (user_id, role_id),
  CONSTRAINT fk_cur_user FOREIGN KEY (user_id) REFERENCES center_user(id),
  CONSTRAINT fk_cur_role FOREIGN KEY (role_id) REFERENCES center_role(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE center_role_permission (
  role_id BIGINT NOT NULL,
  permission_id BIGINT NOT NULL,
  PRIMARY KEY (role_id, permission_id),
  CONSTRAINT fk_crp_role FOREIGN KEY (role_id) REFERENCES center_role(id),
  CONSTRAINT fk_crp_permission FOREIGN KEY (permission_id) REFERENCES center_permission(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE center_client (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  machine_id CHAR(64) NOT NULL,
  machine_name VARCHAR(100) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
  bound_user_id BIGINT NULL,
  bound_at DATETIME(3) NULL,
  online_status VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN',
  last_heartbeat_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_center_client_machine (machine_id),
  KEY idx_center_client_bound_user (bound_user_id),
  CONSTRAINT fk_center_client_user FOREIGN KEY (bound_user_id) REFERENCES center_user(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO center_role (id, role_key, role_name) VALUES
  (1, 'ADMIN', '管理员'), (2, 'OPERATOR', '运营'), (3, 'READ_ONLY', '只读');

INSERT INTO center_permission (id, permission_key, permission_name) VALUES
  (1, 'user:me', '查看当前用户'),
  (2, 'role:read', '查看角色'),
  (3, 'permission:read', '查看权限'),
  (4, 'client:read', '查看客户端'),
  (5, 'client:bind', '绑定客户端'),
  (6, 'user:create', '创建用户');

INSERT INTO center_role_permission (role_id, permission_id) VALUES
  (1,1),(1,2),(1,3),(1,4),(1,5),(1,6),
  (2,1),(2,2),(2,3),(2,4),
  (3,1),(3,2),(3,3);

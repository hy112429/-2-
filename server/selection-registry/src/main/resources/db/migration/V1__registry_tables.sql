CREATE TABLE selection_reservation (
  product_id VARCHAR(96) NOT NULL,
  reservation_token CHAR(36) NOT NULL,
  request_id CHAR(36) NOT NULL,
  account_key VARCHAR(128) NOT NULL,
  leaf_category_id VARCHAR(96) NOT NULL,
  title VARCHAR(512) NULL,
  state VARCHAR(16) NOT NULL,
  created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (product_id),
  UNIQUE KEY uq_reservation_token (reservation_token),
  UNIQUE KEY uq_request_product (request_id, product_id),
  KEY ix_reservation_account_category (account_key, leaf_category_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE account_category_history (
  account_key VARCHAR(128) NOT NULL,
  leaf_category_id VARCHAR(96) NOT NULL,
  product_id VARCHAR(96) NOT NULL,
  state VARCHAR(16) NOT NULL,
  updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (account_key, leaf_category_id),
  UNIQUE KEY uq_category_product (product_id),
  KEY ix_category_usage (leaf_category_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

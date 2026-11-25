-- ====================================================================
-- MySQL 8.0 結構定義 (Schema)
-- 包含：基準線 (Baseline)、設計 A (快照)、設計 B (觸發器歷史表)、設計 C (事件溯源)
-- ====================================================================

-- --------------------------------------------------------------------
-- 0. 基準線對照組 (Baseline)：不做任何版本控制的純主表
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts_baseline (
    id INT NOT NULL,
    name VARCHAR(30) NOT NULL,
    balance INT NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- 1. 設計 A（定期快照 Snapshot）
-- --------------------------------------------------------------------
-- 主表 accounts_a：存放當前最新狀態
CREATE TABLE IF NOT EXISTS accounts_a (
    id INT NOT NULL,
    name VARCHAR(30) NOT NULL,
    balance INT NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 快照表 accounts_snapshot：定期整張表複製進來
CREATE TABLE IF NOT EXISTS accounts_snapshot (
    snapshot_id INT NOT NULL,
    taken_at DATETIME(6) NOT NULL,
    id INT NOT NULL,
    name VARCHAR(30) NOT NULL,
    balance INT NOT NULL,
    PRIMARY KEY (snapshot_id, id),
    INDEX idx_snapshot_taken_at (taken_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- 2. 設計 B（觸發器 ＋ 歷史表 Trigger + History Table）
-- --------------------------------------------------------------------
-- 主表 accounts_b：存放當前最新狀態
CREATE TABLE IF NOT EXISTS accounts_b (
    id INT NOT NULL,
    name VARCHAR(30) NOT NULL,
    balance INT NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 歷史表 accounts_history：儲存被覆寫或刪除的舊資料列
CREATE TABLE IF NOT EXISTS accounts_history (
    history_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    id INT NOT NULL,
    name VARCHAR(30) NOT NULL,
    balance INT NOT NULL,
    valid_from DATETIME(6) NOT NULL,
    valid_to DATETIME(6) NOT NULL,
    INDEX idx_acc_history_id_time (id, valid_from, valid_to),
    INDEX idx_acc_history_range (valid_from, valid_to)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 觸發器：更新前將舊資料寫入歷史表，並更新 valid_to
DROP TRIGGER IF EXISTS trg_accounts_b_before_update;
DELIMITER $$
CREATE TRIGGER trg_accounts_b_before_update
BEFORE UPDATE ON accounts_b
FOR EACH ROW
BEGIN
    -- 若呼叫端未帶入新時間，自動賦予當前微秒精度時間（可由 @tm_override_time 覆寫以利測試）
    IF NEW.updated_at IS NULL OR NEW.updated_at = OLD.updated_at THEN
        SET NEW.updated_at = COALESCE(@tm_override_time, NOW(6));
    END IF;
    INSERT INTO accounts_history (id, name, balance, valid_from, valid_to)
    VALUES (OLD.id, OLD.name, OLD.balance, OLD.updated_at, NEW.updated_at);
END$$
DELIMITER ;

-- 觸發器：刪除前將舊資料寫入歷史表，valid_to 為刪除時刻
DROP TRIGGER IF EXISTS trg_accounts_b_before_delete;
DELIMITER $$
CREATE TRIGGER trg_accounts_b_before_delete
BEFORE DELETE ON accounts_b
FOR EACH ROW
BEGIN
    INSERT INTO accounts_history (id, name, balance, valid_from, valid_to)
    VALUES (OLD.id, OLD.name, OLD.balance, OLD.updated_at, COALESCE(@tm_override_time, NOW(6)));
END$$
DELIMITER ;

-- --------------------------------------------------------------------
-- 3. 設計 C（事件溯源 Event Sourcing ＋ 檢查點 Checkpoints）
-- --------------------------------------------------------------------
-- 唯讀或當前快照主表（可選，便於高速讀取當前狀態）
CREATE TABLE IF NOT EXISTS accounts_c (
    id INT NOT NULL,
    name VARCHAR(30) NOT NULL,
    balance INT NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 事件表 account_events：Append-only 紀錄不可變的所有帳戶變更事件
CREATE TABLE IF NOT EXISTS account_events (
    event_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    account_id INT NOT NULL,
    event_type ENUM('CREATE', 'DEPOSIT', 'WITHDRAW', 'RENAME', 'DELETE') NOT NULL,
    amount INT DEFAULT NULL,
    new_name VARCHAR(30) DEFAULT NULL,
    occurred_at DATETIME(6) NOT NULL,
    INDEX idx_events_acc_time (account_id, occurred_at),
    INDEX idx_events_time (occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 檢查點快照表 account_checkpoints：加速歷史重播計算
CREATE TABLE IF NOT EXISTS account_checkpoints (
    checkpoint_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    last_event_id BIGINT NOT NULL,
    account_id INT NOT NULL,
    name VARCHAR(30) NOT NULL,
    balance INT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    INDEX idx_chkpt_acc_event (account_id, last_event_id),
    INDEX idx_chkpt_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

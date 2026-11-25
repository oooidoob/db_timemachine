-- ====================================================================
-- MariaDB 11.4 結構定義 (Schema)
-- 包含：基準線對照組 (Baseline-MariaDB) 與 設計 D (WITH SYSTEM VERSIONING)
-- ====================================================================

-- 0. MariaDB 基準線對照組 (Baseline-MariaDB)：普通無版本控制的主表
CREATE TABLE IF NOT EXISTS accounts_baseline_mariadb (
    id INT NOT NULL,
    name VARCHAR(30) NOT NULL,
    balance INT NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 1. 原生系統版本化表 accounts_sv
-- 備註：MariaDB 會自動維護隱藏的 row_start 與 row_end 時間戳記
CREATE TABLE IF NOT EXISTS accounts_sv (
    id INT NOT NULL,
    name VARCHAR(30) NOT NULL,
    balance INT NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id)
) WITH SYSTEM VERSIONING;

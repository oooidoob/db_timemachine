"""
通用即時監控模組 (GenericMonitor) 單元測試 (test_generic_monitor.py)
驗證在任意自訂資料庫與任意結構資料表（例如自訂 orders 訂單表）上：
1. 能動態偵測資料庫與資料表、主鍵欄位
2. 一鍵掛載通用觸發器與影子表
3. 外部直接執行 INSERT/UPDATE/DELETE 後，能精確查詢歷史與時間軸
4. 單筆還原與整表還原能正確回復自訂欄位內容
"""

import time
from datetime import datetime
import pytest
from timemachine.db import get_connection
from timemachine.generic_monitor import GenericMonitor


def test_generic_monitor_custom_table_mysql(check_mysql_available):
    """測試在 MySQL 中對任意自訂結構的資料表掛載通用時光機"""
    conn = get_connection("mysql")
    db_name = conn.db.decode() if isinstance(conn.db, bytes) else str(conn.db)
    test_tbl = "custom_orders_test"
    hist_tbl = f"{test_tbl}_tm_history"

    try:
        with conn.cursor() as cur:
            # 清理可能殘留的測試表與觸發器
            cur.execute(f"DROP TRIGGER IF EXISTS `trg_tm_{test_tbl}_ins`;")
            cur.execute(f"DROP TRIGGER IF EXISTS `trg_tm_{test_tbl}_upd`;")
            cur.execute(f"DROP TRIGGER IF EXISTS `trg_tm_{test_tbl}_del`;")
            cur.execute(f"DROP TABLE IF EXISTS `{hist_tbl}`;")
            cur.execute(f"DROP TABLE IF EXISTS `{test_tbl}`;")

            # 建立一張完全不同欄位的自訂資料表
            cur.execute(
                f"""
                CREATE TABLE `{test_tbl}` (
                    order_id INT PRIMARY KEY,
                    customer VARCHAR(40) NOT NULL,
                    total_price INT NOT NULL,
                    status VARCHAR(20) NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
            cur.execute(
                f"INSERT INTO `{test_tbl}` (order_id, customer, total_price, status) VALUES (1001, '王小明', 500, 'PAID');"
            )
        conn.commit()

        # 1. 動態偵測列表與結構
        assert db_name in GenericMonitor.list_databases(conn)
        assert test_tbl in GenericMonitor.list_tables(conn, db_name)
        schema = GenericMonitor.get_table_schema(conn, db_name, test_tbl)
        assert schema["pk_col"] == "order_id"
        assert schema["columns"] == ["order_id", "customer", "total_price", "status"]
        assert GenericMonitor.is_tracking_enabled(conn, db_name, test_tbl, "mysql") is False

        # 2. 一鍵掛載時光機
        init_cnt = GenericMonitor.enable_tracking(conn, db_name, test_tbl, "mysql")
        assert init_cnt == 1
        assert GenericMonitor.is_tracking_enabled(conn, db_name, test_tbl, "mysql") is True

        time.sleep(0.05)
        t_safe = datetime.now()
        time.sleep(0.05)

        # 3. 模擬外部程式直接修改與刪除
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE `{test_tbl}` SET total_price = 9999, status = 'CANCELLED' WHERE order_id = 1001;"
            )
            cur.execute(
                f"INSERT INTO `{test_tbl}` (order_id, customer, total_price, status) VALUES (1002, '陳小華', 300, 'NEW');"
            )
        conn.commit()

        # 4. 查詢歷史時間點 t_safe
        hist_rows = GenericMonitor.query_at(conn, db_name, test_tbl, t_safe, "mysql")
        assert len(hist_rows) == 1
        assert hist_rows[0]["order_id"] == 1001
        assert hist_rows[0]["total_price"] == 500
        assert hist_rows[0]["status"] == "PAID"

        # 5. 整表還原至 t_safe（1001 恢復 500/PAID，1002 應被移除）
        affected = GenericMonitor.restore_all(conn, db_name, test_tbl, t_safe, "mysql")
        assert affected == 2

        current_rows = GenericMonitor.get_current_rows(conn, db_name, test_tbl)
        assert len(current_rows) == 1
        assert current_rows[0]["total_price"] == 500
        assert current_rows[0]["status"] == "PAID"

        # 6. 驗證時間軸紀錄
        timeline = GenericMonitor.get_row_history(conn, db_name, test_tbl, 1001, "mysql")
        assert len(timeline) == 3  # INIT -> UPDATE -> UPDATE (restore)

    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP TRIGGER IF EXISTS `trg_tm_{test_tbl}_ins`;")
            cur.execute(f"DROP TRIGGER IF EXISTS `trg_tm_{test_tbl}_upd`;")
            cur.execute(f"DROP TRIGGER IF EXISTS `trg_tm_{test_tbl}_del`;")
            cur.execute(f"DROP TABLE IF EXISTS `{hist_tbl}`;")
            cur.execute(f"DROP TABLE IF EXISTS `{test_tbl}`;")
        conn.commit()
        conn.close()

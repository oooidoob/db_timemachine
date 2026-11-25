"""
Microsoft SQL Server (MSSQL) 通用監控單元測試 (test_mssql_monitor.py)
若本機尚未安裝 pyodbc 或未啟動 SQL Server 服務，會自動略過測試。
"""

import time
from datetime import datetime
import pytest
from timemachine.mssql_monitor import MssqlMonitor, connect_mssql, HAS_PYODBC


@pytest.fixture
def mssql_conn():
    if not HAS_PYODBC:
        pytest.skip("未安裝 pyodbc，略過 SQL Server 測試")
    try:
        conn = connect_mssql(server="localhost", trusted_connection=True)
    except Exception as e:
        pytest.skip(f"本機 SQL Server 未啟動或無法連線：{e}")
    yield conn
    conn.close()


def test_mssql_temporal_enable_and_restore(mssql_conn):
    """測試在 SQL Server 上動態建立測試表、一鍵啟用 Temporal Table 並還原"""
    test_db = "TimeMachineMssqlTest"
    full_tbl = "dbo.OrdersTest"

    # SQL Server 規定 CREATE/DROP DATABASE 不可在交易內執行，需暫時開啟 autocommit
    mssql_conn.autocommit = True
    cur = mssql_conn.cursor()
    cur.execute(
        f"""
        IF DB_ID('{test_db}') IS NULL
            CREATE DATABASE [{test_db}];
        """
    )
    mssql_conn.autocommit = False

    try:
        cur.execute(
            f"""
            USE [{test_db}];
            IF OBJECT_ID('{full_tbl}', 'U') IS NOT NULL
            BEGIN
                IF EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('{full_tbl}') AND temporal_type = 2)
                    ALTER TABLE {full_tbl} SET (SYSTEM_VERSIONING = OFF);
                DROP TABLE IF EXISTS dbo.OrdersTest_tm_history;
                DROP TABLE IF EXISTS {full_tbl};
            END
            CREATE TABLE {full_tbl} (
                OrderId INT PRIMARY KEY,
                Customer NVARCHAR(50) NOT NULL,
                Amount INT NOT NULL
            );
            INSERT INTO {full_tbl} (OrderId, Customer, Amount) VALUES (1, N'王小明', 1000);
            """
        )
        mssql_conn.commit()

        assert test_db in MssqlMonitor.list_databases(mssql_conn)
        assert full_tbl in MssqlMonitor.list_tables(mssql_conn, test_db)
        assert MssqlMonitor.is_tracking_enabled(mssql_conn, test_db, full_tbl) is False

        # 一鍵啟用 SQL Server 原生時態表
        MssqlMonitor.enable_tracking(mssql_conn, test_db, full_tbl)
        assert MssqlMonitor.is_tracking_enabled(mssql_conn, test_db, full_tbl) is True

        time.sleep(0.2)
        t_safe = datetime.now()
        time.sleep(0.2)

        # 模擬外部 SSMS 執行誤刪（SSMS 預設為 autocommit 模式）
        mssql_conn.autocommit = True
        cur.execute(f"USE [{test_db}];")
        cur.execute(f"DELETE FROM {full_tbl} WHERE OrderId = 1;")
        mssql_conn.autocommit = False
        assert len(MssqlMonitor.get_current_rows(mssql_conn, test_db, full_tbl)) == 0

        # 還原
        ok = MssqlMonitor.restore_one(mssql_conn, test_db, full_tbl, 1, t_safe)
        if not ok:
            hist = MssqlMonitor.get_row_history(mssql_conn, test_db, full_tbl, 1)
            pytest.fail(f"restore_one 回傳 False！t_safe={t_safe}, row_history={hist}")
        curr = MssqlMonitor.get_current_rows(mssql_conn, test_db, full_tbl)
        assert len(curr) == 1
        assert curr[0]["Amount"] == 1000
    finally:
        try:
            mssql_conn.autocommit = True
            cur.execute(f"USE master; ALTER DATABASE [{test_db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{test_db}];")
        except Exception:
            pass

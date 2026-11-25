"""
Pytest 測試組態與共用 Fixtures (conftest.py)
支援 MySQL (設計 A、B、C) 與 MariaDB (設計 D) 的測試環境檢測與 Fixture 注入。
"""

import sys
import os
import pytest

# 將專案根目錄加入模組搜尋路徑
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from timemachine.db import get_connection, execute_sql_file, check_db_health
from timemachine.snapshot import SnapshotTimeMachine
from timemachine.trigger_history import TriggerHistoryTimeMachine
from timemachine.event_sourcing import EventSourcingTimeMachine
from timemachine.native_temporal import NativeTemporalTimeMachine


@pytest.fixture(scope="session")
def check_mysql_available():
    """檢查 MySQL 是否可用；若未啟動則略過或提示清楚中文訊息"""
    health = check_db_health()
    if not health["mysql"]["ok"]:
        pytest.skip(
            f"略過需要 MySQL 的測試：{health['mysql']['message']}\n"
            f"請執行 `docker compose up -d` 啟動資料庫服務以進行測試。"
        )


@pytest.fixture(scope="session")
def check_mariadb_available():
    """檢查 MariaDB 是否可用；若未啟動則略過或提示清楚中文訊息"""
    health = check_db_health()
    if not health["mariadb"]["ok"]:
        pytest.skip(
            f"略過需要 MariaDB 的測試：{health['mariadb']['message']}\n"
            f"請執行 `docker compose up -d` 啟動資料庫服務以進行測試。"
        )


@pytest.fixture(scope="session")
def setup_mysql_schema(check_mysql_available):
    """確保 MySQL 測試資料表與觸發器已初始化"""
    schema_path = os.path.join(BASE_DIR, "schema_mysql.sql")
    conn = get_connection("mysql")
    try:
        execute_sql_file(conn, schema_path)
    finally:
        conn.close()


@pytest.fixture(scope="session")
def setup_mariadb_schema(check_mariadb_available):
    """確保 MariaDB 系統版本化資料表已初始化"""
    schema_path = os.path.join(BASE_DIR, "schema_mariadb.sql")
    conn = get_connection("mariadb")
    try:
        execute_sql_file(conn, schema_path)
    finally:
        conn.close()


@pytest.fixture
def tm_a(setup_mysql_schema):
    """提供乾淨的設計 A (Snapshot) 測試實例"""
    tm = SnapshotTimeMachine()
    tm.reset_data()
    yield tm
    tm.close()


@pytest.fixture
def tm_b(setup_mysql_schema):
    """提供乾淨的設計 B (Trigger + History) 測試實例"""
    tm = TriggerHistoryTimeMachine()
    tm.reset_data()
    yield tm
    tm.close()


@pytest.fixture
def tm_c(setup_mysql_schema):
    """提供乾淨的設計 C (Event Sourcing) 測試實例"""
    tm = EventSourcingTimeMachine()
    tm.reset_data()
    yield tm
    tm.close()


@pytest.fixture
def tm_d(setup_mariadb_schema):
    """提供乾淨的設計 D (MariaDB Native Temporal) 測試實例"""
    tm = NativeTemporalTimeMachine()
    tm.reset_data()
    yield tm
    tm.close()

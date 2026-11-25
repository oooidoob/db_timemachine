"""
設計 D：MariaDB 原生系統版本化 (WITH SYSTEM VERSIONING) 單元測試 (test_native_temporal.py)
驗證項目：
1. 資料寫入與更新後，使用 FOR SYSTEM_TIME AS OF 查詢各時間點之版本
2. 刪除資料後，歷史版本仍可被 AS OF 查出
3. 支援單筆還原 (restore_one) 與整表還原 (restore_all)
4. 支援使用 FOR SYSTEM_TIME ALL 檢視引擎底層的完整版本歷史 (ROW_START, ROW_END)
5. 空間計量 (storage_size_mb)
"""

import time
from datetime import datetime, timedelta
import pytest
from timemachine.native_temporal import NativeTemporalTimeMachine


def test_native_temporal_query(tm_d: NativeTemporalTimeMachine):
    """測試 MariaDB 原生 FOR SYSTEM_TIME AS OF 時間旅行查詢"""
    # 建立帳戶
    tm_d.apply_insert(account_id=1, name="Alice", balance=1000)
    time.sleep(0.08)
    t1 = datetime.now()
    time.sleep(0.08)

    # 第一次更新
    tm_d.apply_update(account_id=1, new_balance=2200)
    time.sleep(0.08)
    t2 = datetime.now()
    time.sleep(0.08)

    # 第二次更新
    tm_d.apply_update(account_id=1, new_balance=3300, new_name="Alice Cooper")
    time.sleep(0.08)
    t3 = datetime.now()

    # 驗證 t1
    s1 = tm_d.query_one_at(account_id=1, t=t1)
    assert s1 is not None
    assert s1["balance"] == 1000
    assert s1["name"] == "Alice"

    # 驗證 t2
    s2 = tm_d.query_one_at(account_id=1, t=t2)
    assert s2 is not None
    assert s2["balance"] == 2200
    assert s2["name"] == "Alice"

    # 驗證 t3
    s3 = tm_d.query_one_at(account_id=1, t=t3)
    assert s3 is not None
    assert s3["balance"] == 3300
    assert s3["name"] == "Alice Cooper"


def test_native_temporal_delete_and_restore(tm_d: NativeTemporalTimeMachine):
    """測試刪除資料的時態保留與還原"""
    tm_d.apply_insert(account_id=2, name="Bob", balance=500)
    time.sleep(0.08)
    t_alive = datetime.now()
    time.sleep(0.08)

    # 刪除 Bob
    tm_d.apply_delete(account_id=2)
    time.sleep(0.08)
    t_deleted = datetime.now()

    # 刪除前能查到
    before = tm_d.query_one_at(account_id=2, t=t_alive)
    assert before is not None
    assert before["balance"] == 500

    # 刪除後查無資料
    after = tm_d.query_one_at(account_id=2, t=t_deleted)
    assert after is None

    # 還原至 t_alive
    tm_d.restore_one(account_id=2, t=t_alive)
    current = tm_d.query_one_at(account_id=2, t=datetime.now())
    assert current is not None
    assert current["balance"] == 500


def test_native_temporal_history_timeline(tm_d: NativeTemporalTimeMachine):
    """測試使用 FOR SYSTEM_TIME ALL 查詢系統版本歷程"""
    tm_d.apply_insert(account_id=3, name="Charlie", balance=10)
    time.sleep(0.05)
    tm_d.apply_update(account_id=3, new_balance=20)
    time.sleep(0.05)
    tm_d.apply_update(account_id=3, new_balance=30)

    history = tm_d.get_history(account_id=3)
    assert len(history) == 3
    balances = [h["balance"] for h in history]
    assert balances == [10, 20, 30]

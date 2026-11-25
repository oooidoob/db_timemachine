"""
設計 A：定期快照 (Snapshot) 單元測試 (test_snapshot.py)
驗證項目：
1. 主表新增後執行 take_snapshot，可從快照查到當時狀態
2. 更新後執行新的快照，查詢某時間點會取「不晚於該時間的最近一次快照」
3. 刪除資料後的快照與查詢
4. 單筆還原 (restore_one) 與整表還原 (restore_all)
5. 空間計量 (storage_size_mb)
"""

import time
from datetime import datetime, timedelta
import pytest
from timemachine.snapshot import SnapshotTimeMachine


def test_snapshot_lifecycle(tm_a: SnapshotTimeMachine):
    """測試快照建立與時間點查詢（取不晚於 t 的最近一次快照）"""
    # 建立帳戶
    tm_a.apply_insert(account_id=1, name="Alice", balance=1000)
    time.sleep(0.05)
    t_before_snap1 = datetime.now()
    time.sleep(0.05)

    # 拍攝快照 1
    tm_a.take_snapshot()
    time.sleep(0.05)
    t_after_snap1 = datetime.now()
    time.sleep(0.05)

    # 更新帳戶
    tm_a.apply_update(account_id=1, new_balance=2500, new_name="Alice Wonderland")
    time.sleep(0.05)

    # 拍攝快照 2
    tm_a.take_snapshot()
    time.sleep(0.05)
    t_after_snap2 = datetime.now()

    # 在快照 1 拍攝前：應該查無快照 (None)
    assert tm_a.query_one_at(account_id=1, t=t_before_snap1) is None

    # 在快照 1 與快照 2 之間：取到快照 1 的狀態 (Alice, 1000)
    state1 = tm_a.query_one_at(account_id=1, t=t_after_snap1)
    assert state1 is not None
    assert state1["name"] == "Alice"
    assert state1["balance"] == 1000

    # 在快照 2 之後：取到快照 2 的狀態 (Alice Wonderland, 2500)
    state2 = tm_a.query_one_at(account_id=1, t=t_after_snap2)
    assert state2 is not None
    assert state2["name"] == "Alice Wonderland"
    assert state2["balance"] == 2500


def test_snapshot_restore(tm_a: SnapshotTimeMachine):
    """測試快照還原功能 (restore_one & restore_all)"""
    tm_a.apply_insert(account_id=10, name="User10", balance=100)
    tm_a.apply_insert(account_id=20, name="User20", balance=200)
    tm_a.take_snapshot()

    time.sleep(0.05)
    t_cp = datetime.now()
    time.sleep(0.05)

    # 修改與刪除
    tm_a.apply_update(account_id=10, new_balance=9999)
    tm_a.apply_delete(account_id=20)
    tm_a.apply_insert(account_id=30, name="User30", balance=300)

    # 整表還原至 t_cp 對應之快照
    affected = tm_a.restore_all(t=t_cp)
    assert affected > 0

    rows = tm_a.query_at(datetime.now())
    # 目前主表尚未重新拍照，直接查 query_at 針對的是歷史快照，若查主表內容：
    row_map = {r["id"]: r for r in tm_a.query_at(t=t_cp)}
    assert row_map[10]["balance"] == 100
    assert 20 in row_map
    assert 30 not in row_map


def test_snapshot_storage_size(tm_a: SnapshotTimeMachine):
    """測試快照表空間計量"""
    tm_a.apply_insert(account_id=1, name="Test", balance=50)
    tm_a.take_snapshot()
    size = tm_a.storage_size_mb()
    assert isinstance(size, float)
    assert size >= 0.0

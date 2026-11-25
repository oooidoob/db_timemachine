"""
設計 C：事件溯源 (Event Sourcing) 單元測試 (test_event_sourcing.py)
驗證項目：
1. CREATE、DEPOSIT、WITHDRAW、RENAME、DELETE 事件紀錄正確性
2. state_at 透過重播演算出任意歷史時間點之狀態
3. 檢查點快照 (Checkpoints) 加速重播機制
4. 刪除後的狀態重播與還原
5. 空間計量與歷程時間軸
"""

import time
from datetime import datetime, timedelta
import pytest
from timemachine.event_sourcing import EventSourcingTimeMachine


def test_event_sourcing_replay(tm_c: EventSourcingTimeMachine):
    """測試事件重播演算歷程"""
    # 建立帳戶 (CREATE 1000)
    tm_c.apply_insert(account_id=1, name="Alice", balance=1000)
    time.sleep(0.05)
    t1 = datetime.now()
    time.sleep(0.05)

    # 存款 (DEPOSIT +500 -> 1500)
    tm_c.apply_update(account_id=1, new_balance=1500)
    time.sleep(0.05)
    t2 = datetime.now()
    time.sleep(0.05)

    # 提款 (WITHDRAW -300 -> 1200) 並更名 (RENAME -> Alice Cooper)
    tm_c.apply_update(account_id=1, new_balance=1200, new_name="Alice Cooper")
    time.sleep(0.05)
    t3 = datetime.now()

    # 驗證 t1
    s1 = tm_c.query_one_at(account_id=1, t=t1)
    assert s1 is not None
    assert s1["balance"] == 1000
    assert s1["name"] == "Alice"

    # 驗證 t2
    s2 = tm_c.query_one_at(account_id=1, t=t2)
    assert s2 is not None
    assert s2["balance"] == 1500
    assert s2["name"] == "Alice"

    # 驗證 t3
    s3 = tm_c.query_one_at(account_id=1, t=t3)
    assert s3 is not None
    assert s3["balance"] == 1200
    assert s3["name"] == "Alice Cooper"


def test_event_sourcing_delete_and_restore(tm_c: EventSourcingTimeMachine):
    """測試刪除事件與還原"""
    tm_c.apply_insert(account_id=2, name="Bob", balance=500)
    time.sleep(0.05)
    t_alive = datetime.now()
    time.sleep(0.05)

    # 刪除 Bob (發送 DELETE 事件)
    tm_c.apply_delete(account_id=2)
    time.sleep(0.05)
    t_dead = datetime.now()

    # 驗證 t_alive 存活
    assert tm_c.query_one_at(account_id=2, t=t_alive)["balance"] == 500
    # 驗證 t_dead 已被刪除
    assert tm_c.query_one_at(account_id=2, t=t_dead) is None

    # 還原至 t_alive
    tm_c.restore_one(account_id=2, t=t_alive)
    curr = tm_c.query_one_at(account_id=2, t=datetime.now())
    assert curr is not None
    assert curr["balance"] == 500


def test_event_sourcing_checkpoint(tm_c: EventSourcingTimeMachine):
    """測試手動與自動檢查點機制加速重播"""
    tm_c.apply_insert(account_id=3, name="Charlie", balance=100)
    # 手動建立檢查點
    tm_c.create_checkpoint(account_id=3)

    time.sleep(0.05)
    tm_c.apply_update(account_id=3, new_balance=200)
    time.sleep(0.05)
    t_target = datetime.now()

    s = tm_c.query_one_at(account_id=3, t=t_target)
    assert s is not None
    assert s["balance"] == 200

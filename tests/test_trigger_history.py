"""
設計 B：觸發器 ＋ 歷史表 (Trigger + History Table) 單元測試 (test_trigger_history.py)
驗證項目：
1. 新增帳戶後可正確查詢當前狀態
2. 更新餘額後，能透過時間點查詢 (query_at / query_one_at) 取得歷史舊值
3. 刪除帳戶後，仍能查詢被刪除前的資料
4. 支援單筆還原 (restore_one)，可將已被刪除或修改的帳戶還原至歷史狀態
5. 支援整表還原 (restore_all)，整張主表回復至過去任意檢查點
6. 支援查詢儲存空間佔用 (storage_size_mb)
7. 完整變更時間軸歷程 (get_history)
"""

import time
from datetime import datetime, timedelta
import pytest
from timemachine.trigger_history import TriggerHistoryTimeMachine


def test_insert_and_query_current(tm_b: TriggerHistoryTimeMachine):
    """測試新增帳戶與當前時間點查詢"""
    now = datetime.now()
    tm_b.apply_insert(account_id=1, name="Alice", balance=1000)

    # 查詢單筆
    acc = tm_b.query_one_at(account_id=1, t=datetime.now())
    assert acc is not None, "新增帳戶後應可查到資料"
    assert acc["id"] == 1
    assert acc["name"] == "Alice"
    assert acc["balance"] == 1000

    # 查詢全表
    all_accs = tm_b.query_at(t=datetime.now())
    assert len(all_accs) == 1
    assert all_accs[0]["name"] == "Alice"


def test_update_and_query_history(tm_b: TriggerHistoryTimeMachine):
    """測試更新帳戶後，各時間點能正確查到當時對應之版本舊值"""
    # 建立帳戶 (Version 1: 1000)
    tm_b.apply_insert(account_id=1, name="Alice", balance=1000)
    time.sleep(0.05)
    t1 = datetime.now()
    time.sleep(0.05)

    # 第一次更新 (Version 2: 2000)
    tm_b.apply_update(account_id=1, new_balance=2000)
    time.sleep(0.05)
    t2 = datetime.now()
    time.sleep(0.05)

    # 第二次更新 (Version 3: 3500, 改名 Alice Cooper)
    tm_b.apply_update(account_id=1, new_balance=3500, new_name="Alice Cooper")
    time.sleep(0.05)
    t3 = datetime.now()

    # 驗證時間點 t1：應為 Version 1 (1000, Alice)
    state_t1 = tm_b.query_one_at(account_id=1, t=t1)
    assert state_t1 is not None
    assert state_t1["balance"] == 1000
    assert state_t1["name"] == "Alice"

    # 驗證時間點 t2：應為 Version 2 (2000, Alice)
    state_t2 = tm_b.query_one_at(account_id=1, t=t2)
    assert state_t2 is not None
    assert state_t2["balance"] == 2000
    assert state_t2["name"] == "Alice"

    # 驗證時間點 t3：應為 Version 3 (3500, Alice Cooper)
    state_t3 = tm_b.query_one_at(account_id=1, t=t3)
    assert state_t3 is not None
    assert state_t3["balance"] == 3500
    assert state_t3["name"] == "Alice Cooper"

    # 驗證帳戶建立前的時間點：應查無此人
    t_before = t1 - timedelta(minutes=5)
    state_before = tm_b.query_one_at(account_id=1, t=t_before)
    assert state_before is None, "建立前的時間點查詢應回傳 None"


def test_delete_and_query_history(tm_b: TriggerHistoryTimeMachine):
    """測試刪除帳戶後，刪除前的時間點仍能查到，刪除後的點查無資料"""
    tm_b.apply_insert(account_id=2, name="Bob", balance=500)
    time.sleep(0.05)
    t_before_delete = datetime.now()
    time.sleep(0.05)

    # 執行刪除
    tm_b.apply_delete(account_id=2)
    time.sleep(0.05)
    t_after_delete = datetime.now()

    # 刪除前：能查到 Bob
    acc_before = tm_b.query_one_at(account_id=2, t=t_before_delete)
    assert acc_before is not None
    assert acc_before["name"] == "Bob"
    assert acc_before["balance"] == 500

    # 刪除後：查不到資料 (None)
    acc_after = tm_b.query_one_at(account_id=2, t=t_after_delete)
    assert acc_after is None, "刪除後的點查詢應為 None"


def test_restore_deleted_account(tm_b: TriggerHistoryTimeMachine):
    """測試單筆還原功能：已被刪除的帳戶能夠還原回來"""
    tm_b.apply_insert(account_id=3, name="Charlie", balance=888)
    time.sleep(0.05)
    t_alive = datetime.now()
    time.sleep(0.05)

    # 刪除 Charlie
    tm_b.apply_delete(account_id=3)

    # 確認目前主表確實已無 Charlie
    current_check = tm_b.query_one_at(account_id=3, t=datetime.now())
    assert current_check is None

    # 執行還原至 t_alive
    restored = tm_b.restore_one(account_id=3, t=t_alive)
    assert restored is True, "應成功執行還原"

    # 驗證目前主表已重新出現 Charlie 且餘額正確
    restored_acc = tm_b.query_one_at(account_id=3, t=datetime.now())
    assert restored_acc is not None
    assert restored_acc["id"] == 3
    assert restored_acc["name"] == "Charlie"
    assert restored_acc["balance"] == 888


def test_restore_updated_account(tm_b: TriggerHistoryTimeMachine):
    """測試單筆還原功能：被更新修改後的帳戶能夠還原至舊餘額"""
    tm_b.apply_insert(account_id=4, name="David", balance=100)
    time.sleep(0.05)
    t_initial = datetime.now()
    time.sleep(0.05)

    # 將餘額改為 9999
    tm_b.apply_update(account_id=4, new_balance=9999)

    # 還原至初始時間點 t_initial
    restored = tm_b.restore_one(account_id=4, t=t_initial)
    assert restored is True

    # 驗證餘額已成功還原為 100
    current_acc = tm_b.query_one_at(account_id=4, t=datetime.now())
    assert current_acc is not None
    assert current_acc["balance"] == 100


def test_restore_all(tm_b: TriggerHistoryTimeMachine):
    """測試整表還原功能：一鍵將全表回復到過去指定時間點"""
    # 建立三筆初始帳戶
    tm_b.apply_insert(account_id=10, name="User10", balance=100)
    tm_b.apply_insert(account_id=20, name="User20", balance=200)
    tm_b.apply_insert(account_id=30, name="User30", balance=300)

    time.sleep(0.05)
    t_checkpoint = datetime.now()
    time.sleep(0.05)

    # 模擬後續一連串雜亂的變動操作：
    # 1. User10 餘額暴增
    tm_b.apply_update(account_id=10, new_balance=9999)
    # 2. User20 被刪除
    tm_b.apply_delete(account_id=20)
    # 3. 新增了 User40
    tm_b.apply_insert(account_id=40, name="User40", balance=400)

    # 執行整表還原至 t_checkpoint
    affected = tm_b.restore_all(t=t_checkpoint)
    assert affected > 0, "整表還原應有變更列數"

    # 驗證目前最新狀態是否與 t_checkpoint 完全一致
    now_all = tm_b.query_at(datetime.now())
    now_map = {r["id"]: r for r in now_all}

    # User10 餘額應回歸 100
    assert 10 in now_map
    assert now_map[10]["balance"] == 100

    # User20 應被復原回來，餘額為 200
    assert 20 in now_map
    assert now_map[20]["balance"] == 200

    # User30 維持 300
    assert 30 in now_map
    assert now_map[30]["balance"] == 300

    # User40 當時並不存在，應已被刪除
    assert 40 not in now_map
    assert len(now_all) == 3


def test_storage_size_mb(tm_b: TriggerHistoryTimeMachine):
    """測試資料表儲存空間計量函式"""
    tm_b.apply_insert(account_id=99, name="TestUser", balance=50)
    size = tm_b.storage_size_mb()
    assert isinstance(size, float)
    assert size >= 0.0


def test_get_history_timeline(tm_b: TriggerHistoryTimeMachine):
    """測試帳戶歷史時間軸軌跡查詢"""
    tm_b.apply_insert(account_id=5, name="Eva", balance=10)
    time.sleep(0.05)
    tm_b.apply_update(account_id=5, new_balance=20)
    time.sleep(0.05)
    tm_b.apply_update(account_id=5, new_balance=30)

    history = tm_b.get_history(account_id=5)
    # 預期包含 2 筆歷史 (valid_to 非空) 與 1 筆目前 (valid_to 為空)
    assert len(history) == 3
    balances = [h["balance"] for h in history]
    assert balances == [10, 20, 30]

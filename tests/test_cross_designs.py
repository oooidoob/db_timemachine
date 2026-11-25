"""
跨設計一致性測試 (test_cross_designs.py)
驗證需求：「四種設計對同一組操作回傳相同結果」。
在相同的操作序列（新增、多次更新、刪除）下，比對各設計在多個時間點的查詢結果是否 100% 吻合。
"""

import time
from datetime import datetime
import pytest
from timemachine.snapshot import SnapshotTimeMachine
from timemachine.trigger_history import TriggerHistoryTimeMachine
from timemachine.event_sourcing import EventSourcingTimeMachine
from timemachine.native_temporal import NativeTemporalTimeMachine
from timemachine.db import check_db_health


def normalize_accounts(rows):
    """將查詢結果規格化為 {id: (name, balance)} 以便嚴謹比對"""
    return {r["id"]: (r["name"], r["balance"]) for r in rows}


def test_cross_design_consistency(setup_mysql_schema):
    """驗證 MySQL 平台上的三種設計 (A:快照, B:觸發器歷史表, C:事件溯源) 結果完全一致"""
    tm_a = SnapshotTimeMachine()
    tm_b = TriggerHistoryTimeMachine()
    tm_c = EventSourcingTimeMachine()

    # 初始化重置資料
    tm_a.reset_data()
    tm_b.reset_data()
    tm_c.reset_data()

    timeline = {}

    try:
        # --- 階段 1：初始建立 ---
        for tm in [tm_a, tm_b, tm_c]:
            tm.apply_insert(101, "Alice", 1000)
            tm.apply_insert(102, "Bob", 2000)
            tm.apply_insert(103, "Charlie", 3000)

        tm_a.take_snapshot()
        time.sleep(0.08)
        timeline["t1"] = datetime.now()
        time.sleep(0.08)

        # --- 階段 2：部分更新 ---
        for tm in [tm_a, tm_b, tm_c]:
            tm.apply_update(101, new_balance=1800)
            tm.apply_update(102, new_balance=2500, new_name="Bob Builder")

        tm_a.take_snapshot()
        time.sleep(0.08)
        timeline["t2"] = datetime.now()
        time.sleep(0.08)

        # --- 階段 3：刪除與新增 ---
        for tm in [tm_a, tm_b, tm_c]:
            tm.apply_delete(102)
            tm.apply_insert(104, "David", 4000)

        tm_a.take_snapshot()
        time.sleep(0.08)
        timeline["t3"] = datetime.now()
        time.sleep(0.08)

        # --- 階段 4：再次更新 ---
        for tm in [tm_a, tm_b, tm_c]:
            tm.apply_update(101, new_balance=900)

        tm_a.take_snapshot()
        time.sleep(0.08)
        timeline["t4"] = datetime.now()

        # --- 驗證四個歷史時間點的全表與單筆查詢 ---
        for t_label, t_val in timeline.items():
            res_a = normalize_accounts(tm_a.query_at(t_val))
            res_b = normalize_accounts(tm_b.query_at(t_val))
            res_c = normalize_accounts(tm_c.query_at(t_val))

            # 斷言三種設計在全表查詢下完全相同
            assert res_b == res_c, f"在時間點 {t_label}，設計 B 與設計 C 結果不一致！\nB: {res_b}\nC: {res_c}"
            assert res_a == res_b, f"在時間點 {t_label}，設計 A 與設計 B 結果不一致！\nA: {res_a}\nB: {res_b}"

            # 斷言單筆查詢一致性
            for acc_id in [101, 102, 103, 104]:
                one_a = tm_a.query_one_at(acc_id, t_val)
                one_b = tm_b.query_one_at(acc_id, t_val)
                one_c = tm_c.query_one_at(acc_id, t_val)

                val_a = (one_a["name"], one_a["balance"]) if one_a else None
                val_b = (one_b["name"], one_b["balance"]) if one_b else None
                val_c = (one_c["name"], one_c["balance"]) if one_c else None

                assert val_b == val_c == val_a, f"在時間點 {t_label} 帳戶 {acc_id} 單筆查詢結果不一致！"

    finally:
        tm_a.close()
        tm_b.close()
        tm_c.close()


def test_cross_design_with_mariadb(setup_mysql_schema, setup_mariadb_schema):
    """若 MariaDB 容器啟用，驗證設計 D 與設計 B/C 結果 100% 相同"""
    health = check_db_health()
    if not health["mariadb"]["ok"]:
        pytest.skip("MariaDB 未啟動，略過與設計 D 之比對")

    tm_b = TriggerHistoryTimeMachine()
    tm_d = NativeTemporalTimeMachine()

    tm_b.reset_data()
    tm_d.reset_data()

    try:
        # 建立
        tm_b.apply_insert(201, "Eva", 500)
        tm_d.apply_insert(201, "Eva", 500)
        time.sleep(0.08)
        t1 = datetime.now()
        time.sleep(0.08)

        # 更新
        tm_b.apply_update(201, new_balance=888)
        tm_d.apply_update(201, new_balance=888)
        time.sleep(0.08)
        t2 = datetime.now()
        time.sleep(0.08)

        # 刪除
        tm_b.apply_delete(201)
        tm_d.apply_delete(201)
        time.sleep(0.08)
        t3 = datetime.now()

        # 比對 t1
        assert normalize_accounts(tm_b.query_at(t1)) == normalize_accounts(tm_d.query_at(t1))
        # 比對 t2
        assert normalize_accounts(tm_b.query_at(t2)) == normalize_accounts(tm_d.query_at(t2))
        # 比對 t3
        assert normalize_accounts(tm_b.query_at(t3)) == normalize_accounts(tm_d.query_at(t3))

    finally:
        tm_b.close()
        tm_d.close()

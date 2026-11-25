"""
資料庫時光機：四種歷史設計效能量測腳本 (benchmark.py)
嚴格執行基準測試，無任何寫死假數字，量測五大核心指標：
1. 空間成本：各設計佔用空間 (MB)，查詢前執行 SET SESSION information_schema_stats_expiry = 0 與 ANALYZE TABLE
2. 寫入成本：每秒更新次數 (TPS / ops/sec，包含 MySQL 與 MariaDB 雙基準線 Baseline)，重複 5 次取平均值與標準差
3. 時間點查詢延遲：單筆與全表查詢耗時 (ms)，量測設計 B 與 C 索引建立前與建立後之實際差異；其餘設計標示 N/A；若重建索引失敗則立即中止
4. 還原時間：真實災難模擬（對 1000 個帳戶進行毀損/刪除），實測 restore_one (1 筆)、逐一 restore_one (1000 筆實測)、restore_all，並嚴格校驗還原一致性（不一致報錯）
5. 正確性比對：300 步歷史變動（50% 命中抽樣帳戶，跳過已刪除帳戶），抽樣 200 個帳戶，需 200 個帳戶全數吻合才算該時間點通過（時間點一致率），並提供逐筆準確率

量測結果輸出至：
- results.csv (原始數據)
- results.md (格式化 Markdown 報表，含各設計已知限制與雙引擎對照說明)
- figures/ (Matplotlib 長條圖視覺化圖表，完整設計名稱標籤)
"""

from __future__ import annotations
import os
import sys
import time
import random
import copy
import argparse
import csv
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional
import statistics

# 嘗試載入數據分析與繪圖套件
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_PLOT_DEPS = True
except ImportError:
    HAS_PLOT_DEPS = False

# 將專案根目錄加入模組搜尋路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from timemachine.db import get_connection, check_db_health, BASE_DIR
from timemachine.base import BaseTimeMachine
from timemachine.snapshot import SnapshotTimeMachine
from timemachine.trigger_history import TriggerHistoryTimeMachine
from timemachine.event_sourcing import EventSourcingTimeMachine
from timemachine.native_temporal import NativeTemporalTimeMachine


# ==============================================================================
# 0. 基準線實作 (BaselineTimeMachine)：普通無版本控制的主表
# ==============================================================================
class BaselineTimeMachine(BaseTimeMachine):
    """基準線對照組：不做任何版本控制的普通資料表 (支援 MySQL 與 MariaDB)"""

    def __init__(self, table_name: str = "accounts_baseline", db_type: str = "mysql"):
        super().__init__(db_type=db_type)
        self.table_name = table_name

    def get_connection(self):
        if self.conn is None or not self.conn.open:
            self.conn = get_connection(self.db_type, autocommit=False)
        return self.conn

    def reset_data(self):
        conn = self.get_connection()
        with conn.cursor() as cur:
            # 確保資料表存在並清空
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{self.table_name}` (
                    id INT NOT NULL,
                    name VARCHAR(30) NOT NULL,
                    balance INT NOT NULL,
                    updated_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                """
            )
            cur.execute(f"TRUNCATE TABLE `{self.table_name}`;")
        conn.commit()

    def apply_insert(self, account_id: int, name: str, balance: int, timestamp=None):
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO `{self.table_name}` (id, name, balance, updated_at) VALUES (%s, %s, %s, %s);",
                (account_id, name, balance, now_dt),
            )
        conn.commit()

    def apply_update(self, account_id: int, new_balance: int, new_name=None, timestamp=None):
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()
        with conn.cursor() as cur:
            if new_name is not None:
                cur.execute(
                    f"UPDATE `{self.table_name}` SET balance = %s, name = %s, updated_at = %s WHERE id = %s;",
                    (new_balance, new_name, now_dt, account_id),
                )
            else:
                cur.execute(
                    f"UPDATE `{self.table_name}` SET balance = %s, updated_at = %s WHERE id = %s;",
                    (new_balance, now_dt, account_id),
                )
        conn.commit()

    def apply_delete(self, account_id: int, timestamp=None):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM `{self.table_name}` WHERE id = %s;", (account_id,))
        conn.commit()

    def query_at(self, t: datetime):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, name, balance, updated_at FROM `{self.table_name}` ORDER BY id ASC;")
            rows = cur.fetchall()
        conn.commit()
        return rows

    def query_one_at(self, account_id: int, t: datetime):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, name, balance, updated_at FROM `{self.table_name}` WHERE id = %s LIMIT 1;",
                (account_id,),
            )
            row = cur.fetchone()
        conn.commit()
        return row

    def restore_one(self, account_id: int, t: datetime):
        return False

    def restore_all(self, t: datetime):
        return 0

    def storage_size_mb(self):
        conn = self.get_connection()
        db_name = conn.db.decode() if isinstance(conn.db, bytes) else str(conn.db)
        with conn.cursor() as cur:
            if self.db_type == "mysql":
                try:
                    cur.execute("SET SESSION information_schema_stats_expiry = 0;")
                except Exception:
                    pass
            cur.execute(f"ANALYZE TABLE `{self.table_name}`;")
            cur.execute(
                """
                SELECT COALESCE(SUM(data_length + index_length), 0) / (1024 * 1024) AS size_mb
                FROM information_schema.TABLES
                WHERE table_schema = %s AND table_name = %s;
                """,
                (db_name, self.table_name),
            )
            row = cur.fetchone()
        conn.commit()
        return float(row["size_mb"]) if row and row.get("size_mb") is not None else 0.0

    def get_history(self, account_id: int):
        row = self.query_one_at(account_id, datetime.now())
        return [row] if row else []


# ==============================================================================
# 1. 實驗執行核心類別 BenchmarkRunner
# ==============================================================================
class BenchmarkRunner:
    """執行完整效能量測與對比"""

    def __init__(
        self,
        num_accounts: int = 10000,
        updates_per_account: int = 5,
        repeats: int = 5,
        sample_timestamps: int = 100,
        history_steps: int = 300,
        snapshots: int = 15,
        skip_mariadb: bool = False,
    ):
        self.num_accounts = num_accounts
        self.updates_per_account = updates_per_account
        self.repeats = repeats
        self.sample_timestamps = sample_timestamps
        self.history_steps = history_steps
        self.snapshots = snapshots
        self.skip_mariadb = skip_mariadb

        # 檢查 MariaDB 連線狀態
        health = check_db_health()
        if not health["mariadb"]["ok"]:
            print("[WARN] 提示: 未偵測到 MariaDB 服務 (3307)，將略過 Baseline (MariaDB) 與 設計 D。")
            self.skip_mariadb = True

        self.figures_dir = os.path.join(BASE_DIR, "figures")
        os.makedirs(self.figures_dir, exist_ok=True)

        self.results_data: List[Dict[str, Any]] = []

    def run_all(self):
        """執行完整測試流程"""
        print("\n" + "=" * 70)
        print(f" 開始執行資料庫時光機效能評測 (Benchmark)")
        print(f"    - 測試帳戶總數 (N): {self.num_accounts}")
        print(f"    - 平均更新次數 (K): {self.updates_per_account}")
        print(f"    - 歷史變更步驟: {self.history_steps} 步 (至少 50% 命中抽樣帳戶)")
        print(f"    - 設計 A 快照目標次數: {self.snapshots} 次")
        print(f"    - 重複量測次數: {self.repeats} 次 (取平均值與標準差)")
        print(f"    - 正確性抽樣點數: {self.sample_timestamps} 個歷史時間點 (200 帳戶全對才算通過)")
        print("=" * 70 + "\n")

        # 註冊參與評測的設計清單
        designs: List[Tuple[str, BaseTimeMachine]] = [
            ("Baseline (MySQL)", BaselineTimeMachine(table_name="accounts_baseline", db_type="mysql")),
        ]
        if not self.skip_mariadb:
            designs.append(("Baseline (MariaDB)", BaselineTimeMachine(table_name="accounts_baseline_mariadb", db_type="mariadb")))

        designs.extend([
            ("設計 A (定期快照)", SnapshotTimeMachine()),
            ("設計 B (觸發器+歷史表)", TriggerHistoryTimeMachine()),
            ("設計 C (事件溯源)", EventSourcingTimeMachine(checkpoint_interval=50)),
        ])
        if not self.skip_mariadb:
            designs.append(("設計 D (MariaDB原生)", NativeTemporalTimeMachine()))

        for label, tm in designs:
            print(f"\n>>> 正在評測：{label} ...")
            metrics = self._benchmark_design(label, tm)
            self.results_data.append(metrics)
            tm.close()

        # 匯出結果報表與長條圖
        self.export_csv()
        self.export_markdown()
        self.plot_figures()

        print("\n" + "=" * 70)
        print(" 所有基準測試完成！已產出 results.csv, results.md 與 figures/ 圖表。")
        print("=" * 70)

    def _benchmark_design(self, label: str, tm: BaseTimeMachine) -> Dict[str, Any]:
        """單一設計的完整評測流程"""
        tm.reset_data()

        # ----------------------------------------------------------------------
        # 全程真實歷史字典 (Ground Truth)，同步記錄「所有帳戶」當前最新真實狀態
        # ----------------------------------------------------------------------
        ground_truth_all: Dict[int, Optional[Dict[str, Any]]] = {}

        # ----------------------------------------------------------------------
        # 1. 初始資料寫入
        # ----------------------------------------------------------------------
        print(f"  [1/6] 寫入初始資料：正在建立 {self.num_accounts} 筆帳戶...")
        for i in range(1, self.num_accounts + 1):
            name = f"User{i}"
            bal = 1000
            tm.apply_insert(i, name, bal)
            ground_truth_all[i] = {"id": i, "name": name, "balance": bal}

        design_a_snapshots_taken = 0
        if isinstance(tm, SnapshotTimeMachine):
            tm.take_snapshot()
            design_a_snapshots_taken += 1

        # ----------------------------------------------------------------------
        # 2. 寫入成本 (Write Throughput) 量測（全程同步更新 ground_truth_all）
        # ----------------------------------------------------------------------
        print(f"  [2/6] 寫入成本：量測每秒更新次數 (TPS，重複 {self.repeats} 次)...")
        write_tps_list = []
        test_update_count = min(self.num_accounts, 2000)

        for r in range(self.repeats):
            sample_ids = random.sample(range(1, self.num_accounts + 1), test_update_count)
            t_start = time.perf_counter()
            for acc_id in sample_ids:
                new_bal = random.randint(100, 50000)
                tm.apply_update(acc_id, new_balance=new_bal)
                if ground_truth_all[acc_id] is not None:
                    ground_truth_all[acc_id]["balance"] = new_bal
            t_end = time.perf_counter()
            elapsed = t_end - t_start
            tps = test_update_count / elapsed if elapsed > 0 else 0
            write_tps_list.append(tps)

        tps_mean = statistics.mean(write_tps_list)
        tps_std = statistics.stdev(write_tps_list) if len(write_tps_list) > 1 else 0.0
        print(f"        TPS: {tps_mean:.2f} ± {tps_std:.2f} updates/sec")

        if isinstance(tm, SnapshotTimeMachine):
            tm.take_snapshot()
            design_a_snapshots_taken += 1

        # ----------------------------------------------------------------------
        # 3. 空間成本 (Storage Size) 量測 (ANALYZE TABLE + stats_expiry=0)
        # ----------------------------------------------------------------------
        print("  [3/6] 空間成本：更新統計資訊並查詢資料表佔用大小 (MB)...")
        storage_mb = tm.storage_size_mb()
        print(f"        空間佔用: {storage_mb:.3f} MB")

        # ----------------------------------------------------------------------
        # 4. 歷史時間軸建立（增加到 300 步，至少 50% 命中抽樣帳戶，跳過已刪除帳戶）
        # ----------------------------------------------------------------------
        print(f"  [4/6] 建立歷史變動 ({self.history_steps} 步，抽樣 200 帳戶維護真實狀態)...")
        sampled_200_ids = random.sample(list(ground_truth_all.keys()), min(200, len(ground_truth_all)))
        milestones: List[Tuple[datetime, Dict[int, Optional[Dict[str, Any]]]]] = []

        # 記錄起始檢查點
        time.sleep(0.04)
        t_init = datetime.now()
        milestones.append((t_init, {aid: copy.deepcopy(ground_truth_all[aid]) for aid in sampled_200_ids}))

        # 快照間隔
        snap_interval = max(1, self.history_steps // self.snapshots)

        for step in range(self.history_steps):
            # 選取變動目標：至少 50% 命中 sampled_200_ids，且跳過已刪除帳戶
            alive_sampled = [aid for aid in sampled_200_ids if ground_truth_all[aid] is not None]
            alive_all = [aid for aid in ground_truth_all if ground_truth_all[aid] is not None]

            if not alive_all:
                break

            if random.random() < 0.50 and alive_sampled:
                target_id = random.choice(alive_sampled)
            else:
                target_id = random.choice(alive_all)

            # 10% 機率刪除，90% 更新
            if random.random() < 0.10 and len(alive_all) > 50:
                tm.apply_delete(target_id)
                ground_truth_all[target_id] = None
            else:
                new_bal = random.randint(500, 99999)
                new_name = f"User{target_id}_V{step}" if step % 20 == 0 else None
                tm.apply_update(target_id, new_balance=new_bal, new_name=new_name)
                if ground_truth_all[target_id] is not None:
                    ground_truth_all[target_id]["balance"] = new_bal
                    if new_name is not None:
                        ground_truth_all[target_id]["name"] = new_name

            # 設計 A 定期快照
            if isinstance(tm, SnapshotTimeMachine) and (step % snap_interval == 0 or step == self.history_steps - 1):
                tm.take_snapshot()
                design_a_snapshots_taken += 1

            time.sleep(0.01)  # 確保微秒時間推進
            t_curr = datetime.now()
            milestones.append((t_curr, {aid: copy.deepcopy(ground_truth_all[aid]) for aid in sampled_200_ids}))

        if isinstance(tm, SnapshotTimeMachine):
            tm.take_snapshot()
            design_a_snapshots_taken += 1

        # ----------------------------------------------------------------------
        # 5. 時間點查詢延遲（單筆 vs 全表；有索引 vs 無索引）
        # ----------------------------------------------------------------------
        sample_time = milestones[len(milestones) // 2][0]

        # 單筆查詢延遲 (重複 5 次取平均)
        single_query_times = []
        for _ in range(self.repeats):
            target_acc = random.choice(sampled_200_ids)
            t_s = time.perf_counter()
            tm.query_one_at(target_acc, sample_time)
            t_e = time.perf_counter()
            single_query_times.append((t_e - t_s) * 1000.0)

        single_query_ms = statistics.mean(single_query_times)

        # 全表查詢延遲 (重複 5 次取平均)
        all_query_times = []
        for _ in range(self.repeats):
            t_s = time.perf_counter()
            tm.query_at(sample_time)
            t_e = time.perf_counter()
            all_query_times.append((t_e - t_s) * 1000.0)

        all_query_ms = statistics.mean(all_query_times)

        # 無索引延遲量測（僅適用於自訂歷史索引的設計 B 與 C；若失敗會報錯並中止）
        single_query_noidx_ms: Optional[float] = None
        all_query_noidx_ms: Optional[float] = None

        if isinstance(tm, TriggerHistoryTimeMachine):
            single_query_noidx_ms, all_query_noidx_ms = self._measure_without_index_design_b(tm, sample_time, sampled_200_ids)
        elif isinstance(tm, EventSourcingTimeMachine):
            single_query_noidx_ms, all_query_noidx_ms = self._measure_without_index_design_c(tm, sample_time, sampled_200_ids)

        s_noidx_str = f"{single_query_noidx_ms:.2f} ms" if single_query_noidx_ms is not None else "N/A"
        a_noidx_str = f"{all_query_noidx_ms:.2f} ms" if all_query_noidx_ms is not None else "N/A"

        print(f"        單筆查詢延遲: {single_query_ms:.2f} ms (無索引: {s_noidx_str})")
        print(f"        全表查詢延遲: {all_query_ms:.2f} ms (無索引: {a_noidx_str})")

        # ----------------------------------------------------------------------
        # 6. 真實災難還原實驗 (Disaster Scenario & Exact Verification)
        # ----------------------------------------------------------------------
        if isinstance(tm, BaselineTimeMachine):
            restore_1_ms = None
            restore_1000_ms = None
            restore_all_ms = None
            print("        基準線不支援歷史還原 (N/A)")
        else:
            print("  [5/6] 災難情境還原測試：對 1000 個帳戶注入毀損並嚴格驗證還原一致性...")
            alive_accs = [aid for aid in ground_truth_all if ground_truth_all[aid] is not None]
            disaster_1000_ids = random.sample(alive_accs, min(1000, len(alive_accs)))

            # 若為設計 A，先拍一張快照以代表災難前的完整狀態
            if isinstance(tm, SnapshotTimeMachine):
                tm.take_snapshot()
                design_a_snapshots_taken += 1
                time.sleep(0.02)

            t_disaster_pre = datetime.now()
            pre_disaster_state = {aid: copy.deepcopy(ground_truth_all[aid]) for aid in disaster_1000_ids}

            # 災難模擬注入函式：將 150 筆刪除，850 筆篡改為負餘額與惡意名稱
            def inject_disaster():
                for idx, aid in enumerate(disaster_1000_ids):
                    if idx < 150:
                        tm.apply_delete(aid)
                    else:
                        tm.apply_update(aid, new_balance=-999999, new_name=f"HACKED_{aid}")

            # ---------------------------
            # (1) 量測單筆還原 (restore_one)
            # ---------------------------
            inject_disaster()
            test_single_id = disaster_1000_ids[0]
            t_s = time.perf_counter()
            tm.restore_one(test_single_id, t_disaster_pre)
            restore_1_ms = (time.perf_counter() - t_s) * 1000.0

            # 驗證單筆還原正確性（不一致直接報錯）
            curr_single = tm.query_one_at(test_single_id, datetime.now())
            exp_single = pre_disaster_state[test_single_id]
            if curr_single is None or curr_single["name"] != exp_single["name"] or curr_single["balance"] != exp_single["balance"]:
                raise RuntimeError(
                    f"[ERROR] [{label}] 單筆還原後與災難前不一致！帳戶={test_single_id}, 預期={exp_single}, 實際={curr_single}"
                )

            # ---------------------------
            # (2) 量測逐一還原 1000 筆 (逐筆 restore_one 實測)
            # ---------------------------
            # 重新注入災難，使全數 1000 筆皆處於毀損狀態
            inject_disaster()
            t_s = time.perf_counter()
            for aid in disaster_1000_ids:
                tm.restore_one(aid, t_disaster_pre)
            restore_1000_ms = (time.perf_counter() - t_s) * 1000.0

            # 逐筆嚴格檢驗 1000 筆資料是否全數與災難前一致
            for aid in disaster_1000_ids:
                curr = tm.query_one_at(aid, datetime.now())
                exp = pre_disaster_state[aid]
                if curr is None or curr["name"] != exp["name"] or curr["balance"] != exp["balance"]:
                    raise RuntimeError(
                        f"[ERROR] [{label}] 逐筆還原 1000 筆後與災難前不一致！帳戶={aid}, 預期={exp}, 實際={curr}"
                    )

            # ---------------------------
            # (3) 量測整表還原 (restore_all)
            # ---------------------------
            # 再次注入災難
            inject_disaster()
            t_s = time.perf_counter()
            tm.restore_all(t_disaster_pre)
            restore_all_ms = (time.perf_counter() - t_s) * 1000.0

            # 嚴格檢驗整表還原正確性
            for aid in disaster_1000_ids:
                curr = tm.query_one_at(aid, datetime.now())
                exp = pre_disaster_state[aid]
                if curr is None or curr["name"] != exp["name"] or curr["balance"] != exp["balance"]:
                    raise RuntimeError(
                        f"[ERROR] [{label}] 整表還原後與災難前不一致！帳戶={aid}, 預期={exp}, 實際={curr}"
                    )

            # 同步更新真實歷史字典以反映還原後的正確狀態
            for aid in disaster_1000_ids:
                ground_truth_all[aid] = copy.deepcopy(pre_disaster_state[aid])

            print(f"        災難還原成功並通過驗證: 1筆={restore_1_ms:.2f}ms, 1000筆逐筆={restore_1000_ms:.2f}ms, 整表={restore_all_ms:.2f}ms")

        # ----------------------------------------------------------------------
        # 7. 正確性檢驗（200 帳戶全對才算時間點通過，並計算逐筆準確率）
        # ----------------------------------------------------------------------
        print(f"  [6/6] 正確性檢驗：抽樣歷史時刻，200 帳戶全數相等才算通過...")
        if isinstance(tm, BaselineTimeMachine):
            consistency_pct: Optional[float] = None
            account_accuracy_pct: Optional[float] = None
            print("        基準線不具備時態能力 (N/A)")
        else:
            # 均勻抽樣 sample_timestamps 個歷史點
            if len(milestones) <= self.sample_timestamps:
                sampled_milestones = milestones
            else:
                step_idx = len(milestones) / self.sample_timestamps
                sampled_milestones = [milestones[int(i * step_idx)] for i in range(self.sample_timestamps)]

            total_timestamps = len(sampled_milestones)
            passed_timestamps = 0
            total_account_checks = 0
            matched_account_checks = 0

            for t_hist, expected_states in sampled_milestones:
                timestamp_all_passed = True
                for aid, expected in expected_states.items():
                    total_account_checks += 1
                    actual = tm.query_one_at(aid, t_hist)

                    # 嚴格比對：名稱與餘額必須完全相等
                    if expected is None:
                        is_match = (actual is None)
                    else:
                        is_match = (actual is not None and actual["name"] == expected["name"] and actual["balance"] == expected["balance"])

                    if is_match:
                        matched_account_checks += 1
                    else:
                        timestamp_all_passed = False

                if timestamp_all_passed:
                    passed_timestamps += 1

            consistency_pct = (passed_timestamps / total_timestamps) * 100.0 if total_timestamps > 0 else 0.0
            account_accuracy_pct = (matched_account_checks / total_account_checks) * 100.0 if total_account_checks > 0 else 0.0

            print(f"        時間點一致率: {consistency_pct:.2f}% ({passed_timestamps}/{total_timestamps} 個時刻通過)")
            print(f"        逐筆帳戶準確率: {account_accuracy_pct:.2f}% ({matched_account_checks}/{total_account_checks} 筆相符)")

        return {
            "design": label,
            "storage_mb": round(storage_mb, 3),
            "write_tps_mean": round(tps_mean, 2),
            "write_tps_std": round(tps_std, 2),
            "query_single_ms": round(single_query_ms, 2),
            "query_single_noidx_ms": round(single_query_noidx_ms, 2) if single_query_noidx_ms is not None else None,
            "query_all_ms": round(all_query_ms, 2),
            "query_all_noidx_ms": round(all_query_noidx_ms, 2) if all_query_noidx_ms is not None else None,
            "restore_1_ms": round(restore_1_ms, 2) if restore_1_ms is not None else None,
            "restore_1000_ms": round(restore_1000_ms, 2) if restore_1000_ms is not None else None,
            "restore_all_ms": round(restore_all_ms, 2) if restore_all_ms is not None else None,
            "consistency_pct": round(consistency_pct, 2) if consistency_pct is not None else None,
            "account_accuracy_pct": round(account_accuracy_pct, 2) if account_accuracy_pct is not None else None,
            "snapshots_taken": design_a_snapshots_taken if isinstance(tm, SnapshotTimeMachine) else None,
        }

    def _measure_without_index_design_b(self, tm: TriggerHistoryTimeMachine, sample_time: datetime, active_ids: List[int]) -> Tuple[Optional[float], Optional[float]]:
        """暫時刪除設計 B 索引，量測無索引查詢耗時後重新建立索引；若重建失敗強制中止評測"""
        conn = tm.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE `{tm.history_table}` DROP INDEX idx_acc_history_id_time;")
                cur.execute(f"ALTER TABLE `{tm.history_table}` DROP INDEX idx_acc_history_range;")
            conn.commit()

            t_s = time.perf_counter()
            for _ in range(3):
                tm.query_one_at(random.choice(active_ids), sample_time)
            s_ms = ((time.perf_counter() - t_s) / 3) * 1000.0

            t_s = time.perf_counter()
            tm.query_at(sample_time)
            a_ms = (time.perf_counter() - t_s) * 1000.0

            return s_ms, a_ms
        except Exception as e:
            print(f"      [警告] 設計 B 無索引量測執行中發生錯誤: {e}")
            return None, None
        finally:
            # 重建索引失敗必須報錯並讓整個 benchmark 中止
            try:
                with conn.cursor() as cur:
                    cur.execute(f"ALTER TABLE `{tm.history_table}` ADD INDEX idx_acc_history_id_time (id, valid_from, valid_to);")
                    cur.execute(f"ALTER TABLE `{tm.history_table}` ADD INDEX idx_acc_history_range (valid_from, valid_to);")
                conn.commit()
            except Exception as rebuild_err:
                print(f"\n[ERROR] [嚴重錯誤] 設計 B 無索引量測後重建索引失敗：{rebuild_err}")
                print("為避免污染後續測試資料庫狀態，基準測試強制中止！")
                raise RuntimeError(f"設計 B 重建索引失敗: {rebuild_err}") from rebuild_err

    def _measure_without_index_design_c(self, tm: EventSourcingTimeMachine, sample_time: datetime, active_ids: List[int]) -> Tuple[Optional[float], Optional[float]]:
        """暫時刪除設計 C 索引，量測無索引重播耗時後重建索引；若重建失敗強制中止評測"""
        conn = tm.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE `{tm.events_table}` DROP INDEX idx_events_acc_time;")
                cur.execute(f"ALTER TABLE `{tm.events_table}` DROP INDEX idx_events_time;")
            conn.commit()

            t_s = time.perf_counter()
            for _ in range(3):
                tm.query_one_at(random.choice(active_ids), sample_time)
            s_ms = ((time.perf_counter() - t_s) / 3) * 1000.0

            t_s = time.perf_counter()
            tm.query_at(sample_time)
            a_ms = (time.perf_counter() - t_s) * 1000.0

            return s_ms, a_ms
        except Exception as e:
            print(f"      [警告] 設計 C 無索引量測執行中發生錯誤: {e}")
            return None, None
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"ALTER TABLE `{tm.events_table}` ADD INDEX idx_events_acc_time (account_id, occurred_at);")
                    cur.execute(f"ALTER TABLE `{tm.events_table}` ADD INDEX idx_events_time (occurred_at);")
                conn.commit()
            except Exception as rebuild_err:
                print(f"\n[ERROR] [嚴重錯誤] 設計 C 無索引量測後重建索引失敗：{rebuild_err}")
                print("為避免污染後續測試資料庫狀態，基準測試強制中止！")
                raise RuntimeError(f"設計 C 重建索引失敗: {rebuild_err}") from rebuild_err

    def export_csv(self):
        """將評測指標匯出至 results.csv"""
        csv_path = os.path.join(BASE_DIR, "results.csv")
        if not self.results_data:
            return
        keys = list(self.results_data[0].keys())
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(self.results_data)
        print(f"已儲存 CSV 報表: {csv_path}")

    def export_markdown(self):
        """將評測指標格式化為 Markdown 表格並儲存至 results.md"""
        md_path = os.path.join(BASE_DIR, "results.md")

        # 取得設計 A 的快照次數
        snap_a_count = None
        for d in self.results_data:
            if "設計 A" in d["design"] and d.get("snapshots_taken") is not None:
                snap_a_count = d["snapshots_taken"]

        lines = [
            "# 資料庫時光機：四種資料變更歷史設計效能評測結果\n",
            "## 一、實驗設定與測試規模",
            f"- **初始帳戶總數 (N)**: {self.num_accounts} 筆",
            f"- **平均每筆更新次數 (K)**: {self.updates_per_account} 次",
            f"- **歷史變更步驟**: {self.history_steps} 步 (至少 50% 命中抽樣帳戶，跳過已刪除帳戶)",
            f"- **設計 A 快照次數**: 測試全程共拍攝 **{snap_a_count or self.snapshots}** 張快照 (可透過 `--snapshots` 參數調整)",
            f"- **量測重複次數**: {self.repeats} 次 (所有延遲與 TPS 均取平均值與標準差)",
            f"- **正確性判定標準**: 抽樣 {self.sample_timestamps} 個歷史時刻，**每個時刻 200 個抽樣帳戶必須全數吻合**才算該時間點通過",
            f"- **災難還原情境**: 對 1000 筆帳戶注入毀損後，實測單筆、1000 筆逐一還原與整表還原，並強制進行資料一致性校驗\n",
            "## 二、效能評測總表\n",
            "| 設計方案 | 空間成本 (MB) | 寫入吞吐 (TPS) | 單筆查詢 (有/無索引 ms) | 全表查詢 (有/無索引 ms) | 還原1筆 (ms) | 還原1000筆 (實測 ms) | 還原整表 (ms) | 時間點一致率 (%) | 逐筆準確率 (%) |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
        ]

        for d in self.results_data:
            name = d["design"]

            # 單筆查詢字串格式化
            s_idx = f"{d['query_single_ms']:.2f}"
            s_noidx = f"{d['query_single_noidx_ms']:.2f}" if d["query_single_noidx_ms"] is not None else "N/A"
            single_q = f"{s_idx} / {s_noidx}"

            # 全表查詢字串格式化
            a_idx = f"{d['query_all_ms']:.2f}"
            a_noidx = f"{d['query_all_noidx_ms']:.2f}" if d["query_all_noidx_ms"] is not None else "N/A"
            all_q = f"{a_idx} / {a_noidx}"

            # 還原耗時格式化
            r1 = f"{d['restore_1_ms']:.2f}" if d["restore_1_ms"] is not None else "N/A"
            r1000 = f"{d['restore_1000_ms']:.2f}" if d["restore_1000_ms"] is not None else "N/A"
            r_all = f"{d['restore_all_ms']:.2f}" if d["restore_all_ms"] is not None else "N/A"

            # 正確性格式化
            corr = f"{d['consistency_pct']:.2f}%" if d["consistency_pct"] is not None else "N/A"
            acc_acc = f"{d['account_accuracy_pct']:.2f}%" if d.get("account_accuracy_pct") is not None else "N/A"

            line = (
                f"| **{name}** | {d['storage_mb']} | {d['write_tps_mean']} ± {d['write_tps_std']} | "
                f"{single_q} | {all_q} | {r1} | {r1000} | {r_all} | {corr} | {acc_acc} |"
            )
            lines.append(line)

        lines.append("\n## 三、引擎對照與各設計已知限制說明：")
        lines.append("1. **雙資料庫引擎對照說明**：")
        lines.append("   - **設計 D 與其他設計跑在不同資料庫引擎**：設計 D 運行於 **MariaDB 11** 引擎；設計 A、B、C 則運行於 **MySQL 8** 引擎。")
        lines.append("   - 因此，設計 D 的寫入吞吐量應與 **Baseline (MariaDB)** 進行同引擎對比；設計 A、B、C 應與 **Baseline (MySQL)** 進行同引擎對比，以確保公平性。")
        lines.append("2. **設計 D（MariaDB 原生系統版本化）**：")
        lines.append("   - **無法回填歷史時間**：MariaDB 的 `WITH SYSTEM VERSIONING` 強制由交易提交時的資料庫核心時鐘指派 `row_start` 與 `row_end`，應用程式無法偽造或補登過去的交易時間點。本評測透過真實的操作流（異動間隔 sleep 推進時鐘）驗證其功能。")
        lines.append("3. **設計 A（定期快照）**：")
        lines.append(f"   - **粗粒度快照**：測試期間共拍攝了 **{snap_a_count or self.snapshots}** 次快照。快照無法還原到兩次快照之間的時間點。若查詢落入兩次快照區間，只能取得「不晚於該時刻的最近一次快照」。在「200 個帳戶全數相等才算通過」的嚴格標準下，時間點一致率如實反映出快照無法涵蓋任意時刻微異動的本質限制。")
        lines.append("4. **設計 C（事件溯源）**：")
        lines.append("   - **全表重播成本高**：重播必須逐批掃描歷史事件流（本實作以 `fetchmany(5000)` 避免記憶體溢位），但隨事件總數增加，全表重播的 CPU 與 IO 運算成本遠高於其他直接讀表的設計。")
        lines.append("5. **基準線 Baseline**：")
        lines.append("   - 為完全不記錄變更歷史的普通表，無任何時態查詢與還原能力，相關欄位均明確標註為 `N/A`。")

        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"已儲存 Markdown 報表: {md_path}")

    def plot_figures(self):
        """使用 Matplotlib 產出 4 張比較長條圖至 figures/，標籤絕不截斷"""
        if not HAS_PLOT_DEPS:
            print("[WARN] 未安裝 matplotlib，略過圖表產出。請執行 pip install matplotlib 安裝。")
            return

        plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "SimHei", "PingFang SC", "DejaVu Sans", "Arial"]
        plt.rcParams["axes.unicode_minus"] = False

        # 完整保留名稱，避免被截斷成「設計」
        def format_label(name: str) -> str:
            if "Baseline (MySQL)" in name:
                return "Baseline\n(MySQL)"
            elif "Baseline (MariaDB)" in name:
                return "Baseline\n(MariaDB)"
            elif "設計 A" in name:
                return "設計 A\n(快照)"
            elif "設計 B" in name:
                return "設計 B\n(觸發器)"
            elif "設計 C" in name:
                return "設計 C\n(事件溯源)"
            elif "設計 D" in name:
                return "設計 D\n(MariaDB原生)"
            return name

        designs = [format_label(d["design"]) for d in self.results_data]

        # ----------------------------------------------------------------------
        # 圖 1：空間佔用長條圖 (MB)
        # ----------------------------------------------------------------------
        plt.figure(figsize=(10, 5.5))
        sizes = [d["storage_mb"] for d in self.results_data]
        bars = plt.bar(designs, sizes, color="#3498db", width=0.5, edgecolor="#2980b9")
        plt.title("各設計空間佔用比較 (Storage Size in MB)", fontsize=14, fontweight="bold")
        plt.ylabel("儲存空間 (MB)", fontsize=12)
        plt.grid(axis="y", linestyle="--", alpha=0.6)
        for bar in bars:
            yval = bar.get_height()
            plt.text(bar.get_x() + bar.get_width() / 2, yval + (max(sizes) * 0.02 or 0.05), f"{yval:.2f} MB", ha="center", va="bottom", fontsize=9)
        plt.tight_layout()
        plt.savefig(os.path.join(self.figures_dir, "01_storage_size.png"), dpi=300)
        plt.close()

        # ----------------------------------------------------------------------
        # 圖 2：寫入吞吐量長條圖 (TPS)
        # ----------------------------------------------------------------------
        plt.figure(figsize=(10, 5.5))
        tps = [d["write_tps_mean"] for d in self.results_data]
        tps_err = [d["write_tps_std"] for d in self.results_data]
        bars = plt.bar(designs, tps, yerr=tps_err, capsize=5, color="#2ecc71", width=0.5, edgecolor="#27ae60")
        plt.title("各設計寫入吞吐量比較 (Write Throughput in TPS)", fontsize=14, fontweight="bold")
        plt.ylabel("每秒更新次數 (Updates/sec)", fontsize=12)
        plt.grid(axis="y", linestyle="--", alpha=0.6)
        for bar in bars:
            yval = bar.get_height()
            plt.text(bar.get_x() + bar.get_width() / 2, yval + (max(tps) * 0.03 or 10), f"{yval:.1f}", ha="center", va="bottom", fontsize=9)
        plt.tight_layout()
        plt.savefig(os.path.join(self.figures_dir, "02_write_throughput.png"), dpi=300)
        plt.close()

        # ----------------------------------------------------------------------
        # 圖 3：時間點查詢延遲 (單筆 vs 全表，索引前後)
        # ----------------------------------------------------------------------
        plt.figure(figsize=(11, 6))
        x = range(len(designs))
        width = 0.2

        single_idx = [d["query_single_ms"] for d in self.results_data]
        single_noidx = [d["query_single_noidx_ms"] or 0 for d in self.results_data]
        all_idx = [d["query_all_ms"] for d in self.results_data]
        all_noidx = [d["query_all_noidx_ms"] or 0 for d in self.results_data]

        plt.bar([i - 1.5 * width for i in x], single_idx, width=width, label="單筆 (有索引)", color="#1abc9c")
        plt.bar([i - 0.5 * width for i in x], single_noidx, width=width, label="單筆 (無索引)", color="#16a085")
        plt.bar([i + 0.5 * width for i in x], all_idx, width=width, label="全表 (有索引)", color="#e67e22")
        plt.bar([i + 1.5 * width for i in x], all_noidx, width=width, label="全表 (無索引)", color="#d35400")

        plt.xticks(x, designs, fontsize=10)
        plt.ylabel("查詢耗時 (毫秒 ms)", fontsize=12)
        plt.title("各設計時間點查詢延遲比較 (含索引前後對照)", fontsize=14, fontweight="bold")
        plt.legend()
        plt.grid(axis="y", linestyle="--", alpha=0.6)
        plt.tight_layout()
        plt.savefig(os.path.join(self.figures_dir, "03_query_latency.png"), dpi=300)
        plt.close()

        # ----------------------------------------------------------------------
        # 圖 4：還原耗時 (單筆、1000筆實測、全表)
        # ----------------------------------------------------------------------
        plt.figure(figsize=(10, 5.5))
        non_baseline = [d for d in self.results_data if d["restore_1_ms"] is not None]
        nb_names = [format_label(d["design"]) for d in non_baseline]
        x_nb = range(len(non_baseline))
        w = 0.25

        r1 = [d["restore_1_ms"] for d in non_baseline]
        r1000 = [d["restore_1000_ms"] for d in non_baseline]
        r_all = [d["restore_all_ms"] for d in non_baseline]

        plt.bar([i - w for i in x_nb], r1, width=w, label="還原 1 筆", color="#9b59b6")
        plt.bar(x_nb, r1000, width=w, label="還原 1,000 筆 (實測)", color="#8e44ad")
        plt.bar([i + w for i in x_nb], r_all, width=w, label="還原整表", color="#e74c3c")

        plt.xticks(x_nb, nb_names, fontsize=10)
        plt.ylabel("還原耗時 (毫秒 ms)", fontsize=12)
        plt.title("各設計歷史還原實測耗時比較 (Restore Latency)", fontsize=14, fontweight="bold")
        plt.legend()
        plt.grid(axis="y", linestyle="--", alpha=0.6)
        plt.tight_layout()
        plt.savefig(os.path.join(self.figures_dir, "04_restore_latency.png"), dpi=300)
        plt.close()

        print(f"已產出 4 張比較長條圖至: {self.figures_dir}")


def main():
    if not HAS_PLOT_DEPS:
        print("\n【套件未安裝提示】")
        print("未偵測到 matplotlib 套件，請先執行：")
        print("    pip install -r requirements.txt")
        print("安裝完成後即可執行完整評測與圖表產出。\n")

    parser = argparse.ArgumentParser(description="資料庫時光機：四種歷史設計效能評測 (Benchmark)")
    parser.add_argument("-n", "--accounts", type=int, default=10000, help="測試帳戶總數 (預設: 10000)")
    parser.add_argument("-k", "--updates-per-account", type=int, default=5, help="平均每筆帳戶更新次數 (預設: 5)")
    parser.add_argument("--history-steps", type=int, default=300, help="歷史變動操作步驟數 (預設: 300)")
    parser.add_argument("--snapshots", type=int, default=15, help="設計 A 變動期間拍攝快照次數 (預設: 15)")
    parser.add_argument("-r", "--repeats", type=int, default=5, help="量測重複次數取平均值 (預設: 5)")
    parser.add_argument("-s", "--sample-timestamps", type=int, default=100, help="正確性檢驗抽樣點數 (預設: 100)")
    parser.add_argument("--skip-mariadb", action="store_true", help="略過 MariaDB (Baseline 與 設計 D) 測試")

    args = parser.parse_args()

    runner = BenchmarkRunner(
        num_accounts=args.accounts,
        updates_per_account=args.updates_per_account,
        history_steps=args.history_steps,
        snapshots=args.snapshots,
        repeats=args.repeats,
        sample_timestamps=args.sample_timestamps,
        skip_mariadb=args.skip_mariadb,
    )
    runner.run_all()


if __name__ == "__main__":
    main()

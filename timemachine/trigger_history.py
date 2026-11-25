"""
設計 B：觸發器 ＋ 歷史表 (Trigger + History Table) 實作模組
利用 BEFORE UPDATE 與 BEFORE DELETE 觸發器，在主表變更時自動將舊版本寫入歷史表。
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Any, Optional
import pymysql

from .base import BaseTimeMachine
from .db import get_connection, execute_sql_file, BASE_DIR
import os


class TriggerHistoryTimeMachine(BaseTimeMachine):
    """
    設計 B：觸發器 ＋ 歷史表時光機實作
    - 主表 (accounts_b): 儲存當前最新狀態 (id, name, balance, updated_at)
    - 歷史表 (accounts_history): 儲存歷次變更的舊資料 (id, name, balance, valid_from, valid_to)
    """

    def __init__(
        self,
        main_table: str = "accounts_b",
        history_table: str = "accounts_history",
        db_type: str = "mysql",
    ):
        super().__init__(db_type=db_type)
        self.main_table = main_table
        self.history_table = history_table

    def get_connection(self) -> pymysql.Connection:
        """取得或建立 MySQL 連線"""
        if self.conn is None or not self.conn.open:
            self.conn = get_connection(self.db_type, autocommit=False)
        return self.conn

    def reset_data(self) -> None:
        """清空主表與歷史表資料（供測試與實驗初始化使用）"""
        conn = self.get_connection()
        with conn.cursor() as cur:
            # 依序清空歷史表記錄與主表記錄
            cur.execute(f"TRUNCATE TABLE `{self.history_table}`;")
            cur.execute(f"TRUNCATE TABLE `{self.main_table}`;")
        conn.commit()

    def apply_insert(
        self,
        account_id: int,
        name: str,
        balance: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """
        新增帳戶（初始寫入）。
        INSERT 不會觸發 BEFORE UPDATE / DELETE，直接寫入主表。
        """
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()
        sql = f"""
            INSERT INTO `{self.main_table}` (id, name, balance, updated_at)
            VALUES (%s, %s, %s, %s);
        """
        with conn.cursor() as cur:
            cur.execute(sql, (account_id, name, balance, now_dt))
        conn.commit()

    def apply_update(
        self,
        account_id: int,
        new_balance: int,
        new_name: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """
        更新帳戶餘額或名稱。
        此更新會觸發 BEFORE UPDATE 觸發器，將原有的舊值移入 accounts_history。
        時間戳記由 Python datetime.now() 提供，保證與本地時區一致。
        """
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()

        with conn.cursor() as cur:
            cur.execute("SET @tm_override_time = %s;", (now_dt,))
            if new_name is not None:
                sql = f"""
                    UPDATE `{self.main_table}`
                    SET balance = %s, name = %s, updated_at = %s
                    WHERE id = %s;
                """
                cur.execute(sql, (new_balance, new_name, now_dt, account_id))
            else:
                sql = f"""
                    UPDATE `{self.main_table}`
                    SET balance = %s, updated_at = %s
                    WHERE id = %s;
                """
                cur.execute(sql, (new_balance, now_dt, account_id))
            cur.execute("SET @tm_override_time = NULL;")
        conn.commit()

    def apply_delete(
        self,
        account_id: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """
        刪除指定帳戶。
        此操作會觸發 BEFORE DELETE 觸發器，將當前值作為最後一筆歷史記錄存入 accounts_history。
        時間戳記由 Python datetime.now() 提供，保證與本地時區一致。
        """
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()
        with conn.cursor() as cur:
            cur.execute("SET @tm_override_time = %s;", (now_dt,))
            sql = f"DELETE FROM `{self.main_table}` WHERE id = %s;"
            cur.execute(sql, (account_id,))
            cur.execute("SET @tm_override_time = NULL;")
        conn.commit()

    def query_at(self, t: datetime) -> List[Dict[str, Any]]:
        """
        時間點查詢：回傳時間點 t 整張主表的所有帳戶狀態。
        結束時執行 conn.commit() 避免 REPEATABLE READ 快照讀取舊版本。
        """
        conn = self.get_connection()
        sql = f"""
            SELECT id, name, balance, valid_from AS updated_at
            FROM `{self.history_table}`
            WHERE valid_from <= %s AND %s < valid_to
            UNION ALL
            SELECT id, name, balance, updated_at
            FROM `{self.main_table}`
            WHERE updated_at <= %s
            ORDER BY id ASC;
        """
        with conn.cursor() as cur:
            cur.execute(sql, (t, t, t))
            results = cur.fetchall()
        conn.commit()
        return results

    def query_one_at(self, account_id: int, t: datetime) -> Optional[Dict[str, Any]]:
        """
        單筆時間點查詢：回傳時間點 t 指定帳戶的狀態。
        結束時執行 conn.commit() 避免 REPEATABLE READ 快照讀取舊版本。
        """
        conn = self.get_connection()
        sql = f"""
            SELECT id, name, balance, valid_from AS updated_at
            FROM `{self.history_table}`
            WHERE id = %s AND valid_from <= %s AND %s < valid_to
            UNION ALL
            SELECT id, name, balance, updated_at
            FROM `{self.main_table}`
            WHERE id = %s AND updated_at <= %s
            LIMIT 1;
        """
        with conn.cursor() as cur:
            cur.execute(sql, (account_id, t, t, account_id, t))
            row = cur.fetchone()
        conn.commit()
        return row

    def restore_one(self, account_id: int, t: datetime) -> bool:
        """
        單筆還原：把指定帳戶還原至時間點 t 的狀態。
        - 目標狀態存在：更新主表或重新插入（觸發器會記錄這次還原動作自身為新版本）。
        - 目標狀態不存在：若主表現有該帳戶則予以刪除。
        """
        target = self.query_one_at(account_id, t)
        conn = self.get_connection()

        with conn.cursor() as cur:
            # 查詢目前主表狀態
            cur.execute(
                f"SELECT id, name, balance FROM `{self.main_table}` WHERE id = %s;",
                (account_id,),
            )
            current = cur.fetchone()

            now_dt = datetime.now()

            if target is not None:
                if current is not None:
                    # 目前存在且值已有變動，執行 UPDATE 還原
                    if current["name"] == target["name"] and current["balance"] == target["balance"]:
                        return False
                    cur.execute(
                        f"""
                        UPDATE `{self.main_table}`
                        SET name = %s, balance = %s, updated_at = %s
                        WHERE id = %s;
                        """,
                        (target["name"], target["balance"], now_dt, account_id),
                    )
                    conn.commit()
                    return True
                else:
                    # 原本已被刪除，重新 INSERT 還原回來
                    cur.execute(
                        f"""
                        INSERT INTO `{self.main_table}` (id, name, balance, updated_at)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (target["id"], target["name"], target["balance"], now_dt),
                    )
                    conn.commit()
                    return True
            else:
                # 目標時間點不存在此帳戶，若目前主表有則刪除
                if current is not None:
                    cur.execute(
                        f"DELETE FROM `{self.main_table}` WHERE id = %s;",
                        (account_id,),
                    )
                    conn.commit()
                    return True
                return False

    def restore_all(self, t: datetime) -> int:
        """
        整表還原：將整張主表還原至時間點 t 的狀態。
        在單一交易中比對目標快照與目前主表，同步執行更新、插入與刪除。
        回傳被變更的總列數。
        """
        targets = self.query_at(t)
        target_map = {row["id"]: row for row in targets}

        conn = self.get_connection()
        affected_count = 0
        now_dt = datetime.now()

        try:
            with conn.cursor() as cur:
                # 取出目前主表所有帳戶
                cur.execute(f"SELECT id, name, balance FROM `{self.main_table}`;")
                current_rows = cur.fetchall()
                current_map = {row["id"]: row for row in current_rows}

                # 處理目標時間點存在的列（插入或更新）
                for acc_id, t_row in target_map.items():
                    if acc_id in current_map:
                        c_row = current_map[acc_id]
                        if c_row["name"] != t_row["name"] or c_row["balance"] != t_row["balance"]:
                            cur.execute(
                                f"""
                                UPDATE `{self.main_table}`
                                SET name = %s, balance = %s, updated_at = %s
                                WHERE id = %s;
                                """,
                                (t_row["name"], t_row["balance"], now_dt, acc_id),
                            )
                            affected_count += 1
                    else:
                        cur.execute(
                            f"""
                            INSERT INTO `{self.main_table}` (id, name, balance, updated_at)
                            VALUES (%s, %s, %s, %s);
                            """,
                            (t_row["id"], t_row["name"], t_row["balance"], now_dt),
                        )
                        affected_count += 1

                # 處理目標時間點不存在但目前存在的列（刪除）
                for acc_id in current_map:
                    if acc_id not in target_map:
                        cur.execute(
                            f"DELETE FROM `{self.main_table}` WHERE id = %s;",
                            (acc_id,),
                        )
                        affected_count += 1

            conn.commit()
            return affected_count
        except Exception:
            conn.rollback()
            raise

    def storage_size_mb(self) -> float:
        """
        計算設計 B 所佔用的儲存空間 (MB)。
        加總 accounts_b 與 accounts_history 的 data_length 與 index_length。
        """
        conn = self.get_connection()
        db_name = conn.db.decode() if isinstance(conn.db, bytes) else str(conn.db)

        sql = """
            SELECT COALESCE(SUM(data_length + index_length), 0) / (1024 * 1024) AS size_mb
            FROM information_schema.TABLES
            WHERE table_schema = %s AND table_name IN (%s, %s);
        """
        with conn.cursor() as cur:
            # 確保讀到最新統計資訊：關閉 MySQL 8 統計快取並執行 ANALYZE TABLE
            try:
                cur.execute("SET SESSION information_schema_stats_expiry = 0;")
            except Exception:
                pass
            cur.execute(f"ANALYZE TABLE `{self.main_table}`, `{self.history_table}`;")
            cur.execute(sql, (db_name, self.main_table, self.history_table))
            row = cur.fetchone()
            conn.commit()
            if row and row.get("size_mb") is not None:
                return float(row["size_mb"])
        return 0.0

    def get_history(self, account_id: int) -> List[Dict[str, Any]]:
        """
        取得單一帳戶的完整時間軸軌跡（包含已移入歷史表與目前主表的版本）。
        結束時執行 conn.commit() 確保讀到最新狀態。
        """
        conn = self.get_connection()
        sql = f"""
            SELECT id, name, balance, valid_from, valid_to, 'history' AS record_type
            FROM `{self.history_table}`
            WHERE id = %s
            UNION ALL
            SELECT id, name, balance, updated_at AS valid_from, NULL AS valid_to, 'current' AS record_type
            FROM `{self.main_table}`
            WHERE id = %s
            ORDER BY valid_from ASC;
        """
        with conn.cursor() as cur:
            cur.execute(sql, (account_id, account_id))
            rows = cur.fetchall()
        conn.commit()
        return rows

"""
設計 A：定期快照 (Snapshot) 實作模組
採用整表週期性快照複製策略。
查詢特定時間點時，取「不晚於該時間點 (<= t) 的最近一次快照」。
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Any, Optional
import pymysql

from .base import BaseTimeMachine
from .db import get_connection


class SnapshotTimeMachine(BaseTimeMachine):
    """
    設計 A：快照時光機實作
    - 主表 (accounts_a): 存放當前最新狀態 (id, name, balance, updated_at)
    - 快照表 (accounts_snapshot): 存放定期整表複製的快照 (snapshot_id, taken_at, id, name, balance)
    """

    def __init__(
        self,
        main_table: str = "accounts_a",
        snapshot_table: str = "accounts_snapshot",
        db_type: str = "mysql",
        auto_snapshot: bool = False,
    ):
        super().__init__(db_type=db_type)
        self.main_table = main_table
        self.snapshot_table = snapshot_table
        self.auto_snapshot = auto_snapshot

    def get_connection(self) -> pymysql.Connection:
        """取得或建立 MySQL 連線"""
        if self.conn is None or not self.conn.open:
            self.conn = get_connection(self.db_type, autocommit=False)
        return self.conn

    def reset_data(self) -> None:
        """清空主表與快照表資料"""
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE `{self.snapshot_table}`;")
            cur.execute(f"TRUNCATE TABLE `{self.main_table}`;")
        conn.commit()

    def take_snapshot(self, timestamp: Optional[datetime] = None) -> int:
        """
        執行整表快照複製：
        將目前主表 accounts_a 的所有資料列完整複製一份至 accounts_snapshot。
        :param timestamp: 指定快照時間（若無則取系統目前微秒時間）
        :return: 該快照複製的資料列筆數
        """
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()

        with conn.cursor() as cur:
            # 取得下一個快照識別碼 snapshot_id
            cur.execute(
                f"SELECT COALESCE(MAX(snapshot_id), 0) + 1 AS next_id FROM `{self.snapshot_table}`;"
            )
            next_id = cur.fetchone()["next_id"]

            # 整表複製至快照表
            insert_sql = f"""
                INSERT INTO `{self.snapshot_table}` (snapshot_id, taken_at, id, name, balance)
                SELECT %s, %s, id, name, balance
                FROM `{self.main_table}`;
            """
            rows = cur.execute(insert_sql, (next_id, now_dt))
        conn.commit()
        return rows

    def apply_insert(
        self,
        account_id: int,
        name: str,
        balance: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """新增帳戶至主表"""
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()
        sql = f"""
            INSERT INTO `{self.main_table}` (id, name, balance, updated_at)
            VALUES (%s, %s, %s, %s);
        """
        with conn.cursor() as cur:
            cur.execute(sql, (account_id, name, balance, now_dt))
        conn.commit()

        if self.auto_snapshot:
            self.take_snapshot(now_dt)

    def apply_update(
        self,
        account_id: int,
        new_balance: int,
        new_name: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """更新帳戶餘額或名稱"""
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()

        with conn.cursor() as cur:
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
        conn.commit()

        if self.auto_snapshot:
            self.take_snapshot(now_dt)

    def apply_delete(
        self,
        account_id: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """從主表刪除帳戶"""
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()
        with conn.cursor() as cur:
            sql = f"DELETE FROM `{self.main_table}` WHERE id = %s;"
            cur.execute(sql, (account_id,))
        conn.commit()

        if self.auto_snapshot:
            self.take_snapshot(now_dt)

    def query_at(self, t: datetime) -> List[Dict[str, Any]]:
        """
        時間點查詢：取得不晚於時間點 t (taken_at <= t) 的最近一次快照整表內容。
        結束時執行 conn.commit() 避免 REPEATABLE READ 讀取舊快照。
        """
        conn = self.get_connection()
        with conn.cursor() as cur:
            # 找出不晚於 t 的最新一次快照 ID
            cur.execute(
                f"""
                SELECT snapshot_id, taken_at
                FROM `{self.snapshot_table}`
                WHERE taken_at <= %s
                ORDER BY taken_at DESC, snapshot_id DESC
                LIMIT 1;
                """,
                (t,),
            )
            snapshot_info = cur.fetchone()

            if not snapshot_info:
                conn.commit()
                return []

            target_snap_id = snapshot_info["snapshot_id"]
            snap_time = snapshot_info["taken_at"]

            # 取出該快照的所有帳戶資料
            cur.execute(
                f"""
                SELECT id, name, balance, %s AS updated_at
                FROM `{self.snapshot_table}`
                WHERE snapshot_id = %s
                ORDER BY id ASC;
                """,
                (snap_time, target_snap_id),
            )
            rows = cur.fetchall()

        conn.commit()
        return rows

    def query_one_at(self, account_id: int, t: datetime) -> Optional[Dict[str, Any]]:
        """
        單筆時間點查詢：取得不晚於時間點 t 的最近一次快照中指定帳戶的狀態。
        若該快照中不存在或該時間點前無任何快照，回傳 None。
        """
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT snapshot_id, taken_at
                FROM `{self.snapshot_table}`
                WHERE taken_at <= %s
                ORDER BY taken_at DESC, snapshot_id DESC
                LIMIT 1;
                """,
                (t,),
            )
            snapshot_info = cur.fetchone()

            if not snapshot_info:
                conn.commit()
                return None

            target_snap_id = snapshot_info["snapshot_id"]
            snap_time = snapshot_info["taken_at"]

            cur.execute(
                f"""
                SELECT id, name, balance, %s AS updated_at
                FROM `{self.snapshot_table}`
                WHERE snapshot_id = %s AND id = %s
                LIMIT 1;
                """,
                (snap_time, target_snap_id, account_id),
            )
            row = cur.fetchone()

        conn.commit()
        return row

    def restore_one(self, account_id: int, t: datetime) -> bool:
        """
        單筆還原：將指定帳戶還原至時間點 t 所對應快照的狀態。
        """
        target = self.query_one_at(account_id, t)
        conn = self.get_connection()

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, name, balance FROM `{self.main_table}` WHERE id = %s;",
                (account_id,),
            )
            current = cur.fetchone()
            now_dt = datetime.now()

            if target is not None:
                if current is not None:
                    if current["name"] == target["name"] and current["balance"] == target["balance"]:
                        conn.commit()
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
                if current is not None:
                    cur.execute(
                        f"DELETE FROM `{self.main_table}` WHERE id = %s;",
                        (account_id,),
                    )
                    conn.commit()
                    return True
                conn.commit()
                return False

    def restore_all(self, t: datetime) -> int:
        """
        整表還原：將整張主表還原至時間點 t 所對應快照的狀態。
        回傳被變更的總列數。
        """
        targets = self.query_at(t)
        target_map = {row["id"]: row for row in targets}

        conn = self.get_connection()
        affected_count = 0
        now_dt = datetime.now()

        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT id, name, balance FROM `{self.main_table}`;")
                current_rows = cur.fetchall()
                current_map = {row["id"]: row for row in current_rows}

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
        """計算設計 A（主表 ＋ 快照表）的總佔用空間 (MB)"""
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
            cur.execute(f"ANALYZE TABLE `{self.main_table}`, `{self.snapshot_table}`;")
            cur.execute(sql, (db_name, self.main_table, self.snapshot_table))
            row = cur.fetchone()
            conn.commit()
            if row and row.get("size_mb") is not None:
                return float(row["size_mb"])
        return 0.0

    def get_history(self, account_id: int) -> List[Dict[str, Any]]:
        """取得單一帳戶在歷次快照與當前主表中的時間軸紀錄"""
        conn = self.get_connection()
        sql = f"""
            SELECT id, name, balance, taken_at AS valid_from, NULL AS valid_to, 'snapshot' AS record_type
            FROM `{self.snapshot_table}`
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

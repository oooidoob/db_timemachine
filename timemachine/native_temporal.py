"""
設計 D：MariaDB 原生系統版本化 (WITH SYSTEM VERSIONING) 實作模組
利用 MariaDB 11 原生支援的 SQL:2011 時態功能。
底層引擎自動維護 row_start 與 row_end，使用 FOR SYSTEM_TIME AS OF 進行時間點查詢。
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Any, Optional
import pymysql

from .base import BaseTimeMachine
from .db import get_connection


class NativeTemporalTimeMachine(BaseTimeMachine):
    """
    設計 D：MariaDB 原生系統版本化時光機實作
    - 資料表 (accounts_sv): 宣告 WITH SYSTEM VERSIONING
    - 查詢語法: SELECT ... FROM accounts_sv FOR SYSTEM_TIME AS OF <timestamp>
    - 歷史追蹤: SELECT ... FROM accounts_sv FOR SYSTEM_TIME ALL
    """

    def __init__(
        self,
        table_name: str = "accounts_sv",
        db_type: str = "mariadb",
    ):
        super().__init__(db_type=db_type)
        self.table_name = table_name

    def get_connection(self) -> pymysql.Connection:
        """取得或建立 MariaDB 連線（預設連線至 3307 埠號）"""
        if self.conn is None or not self.conn.open:
            self.conn = get_connection(self.db_type, autocommit=False)
        return self.conn

    def reset_data(self) -> None:
        """
        清空資料表及其所有底層版本歷史。
        在 MariaDB 中，直接 TRUNCATE 系統版本化資料表可能受限，
        此處透過暫時卸除版本控制後清空，再重新掛載版本控制以確保徹底清空。
        """
        conn = self.get_connection()
        with conn.cursor() as cur:
            try:
                cur.execute(f"ALTER TABLE `{self.table_name}` DROP SYSTEM VERSIONING;")
                cur.execute(f"TRUNCATE TABLE `{self.table_name}`;")
                cur.execute(f"ALTER TABLE `{self.table_name}` ADD SYSTEM VERSIONING;")
            except Exception:
                # 若版本控制切換失敗，退回使用刪除重建
                cur.execute(f"DROP TABLE IF EXISTS `{self.table_name}`;")
                cur.execute(
                    f"""
                    CREATE TABLE `{self.table_name}` (
                        id INT NOT NULL,
                        name VARCHAR(30) NOT NULL,
                        balance INT NOT NULL,
                        updated_at DATETIME(6) NOT NULL,
                        PRIMARY KEY (id)
                    ) WITH SYSTEM VERSIONING;
                    """
                )
        conn.commit()

    def apply_insert(
        self,
        account_id: int,
        name: str,
        balance: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """新增帳戶至系統版本化主表"""
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()
        sql = f"""
            INSERT INTO `{self.table_name}` (id, name, balance, updated_at)
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
        """更新帳戶餘額或名稱（MariaDB 自動將舊列轉移為歷史版本）"""
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()

        with conn.cursor() as cur:
            if new_name is not None:
                sql = f"""
                    UPDATE `{self.table_name}`
                    SET balance = %s, name = %s, updated_at = %s
                    WHERE id = %s;
                """
                cur.execute(sql, (new_balance, new_name, now_dt, account_id))
            else:
                sql = f"""
                    UPDATE `{self.table_name}`
                    SET balance = %s, updated_at = %s
                    WHERE id = %s;
                """
                cur.execute(sql, (new_balance, now_dt, account_id))
        conn.commit()

    def apply_delete(
        self,
        account_id: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """刪除帳戶（MariaDB 將此筆紀錄終止並標記 row_end 為當前交易時間）"""
        conn = self.get_connection()
        with conn.cursor() as cur:
            sql = f"DELETE FROM `{self.table_name}` WHERE id = %s;"
            cur.execute(sql, (account_id,))
        conn.commit()

    def query_at(self, t: datetime) -> List[Dict[str, Any]]:
        """
        時間點查詢：使用 FOR SYSTEM_TIME AS OF 查詢時間點 t 當下的全表狀態。
        結束時執行 conn.commit() 避免 REPEATABLE READ 讀取舊快照。
        """
        conn = self.get_connection()
        t_str = t.strftime("%Y-%m-%d %H:%M:%S.%f") if isinstance(t, datetime) else str(t)

        sql = f"""
            SELECT id, name, balance, updated_at
            FROM `{self.table_name}` FOR SYSTEM_TIME AS OF %s
            ORDER BY id ASC;
        """
        with conn.cursor() as cur:
            cur.execute(sql, (t_str,))
            rows = cur.fetchall()
        conn.commit()
        return rows

    def query_one_at(self, account_id: int, t: datetime) -> Optional[Dict[str, Any]]:
        """
        單筆時間點查詢：使用 FOR SYSTEM_TIME AS OF 查詢時間點 t 該帳戶的狀態。
        """
        conn = self.get_connection()
        t_str = t.strftime("%Y-%m-%d %H:%M:%S.%f") if isinstance(t, datetime) else str(t)

        sql = f"""
            SELECT id, name, balance, updated_at
            FROM `{self.table_name}` FOR SYSTEM_TIME AS OF %s
            WHERE id = %s
            LIMIT 1;
        """
        with conn.cursor() as cur:
            cur.execute(sql, (t_str, account_id))
            row = cur.fetchone()
        conn.commit()
        return row

    def restore_one(self, account_id: int, t: datetime) -> bool:
        """單筆還原：查詢時間點 t 狀態，並在主表套用對應的更新、重新插入或刪除"""
        target = self.query_one_at(account_id, t)
        conn = self.get_connection()

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, name, balance FROM `{self.table_name}` WHERE id = %s;",
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
                        UPDATE `{self.table_name}`
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
                        INSERT INTO `{self.table_name}` (id, name, balance, updated_at)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (target["id"], target["name"], target["balance"], now_dt),
                    )
                    conn.commit()
                    return True
            else:
                if current is not None:
                    cur.execute(
                        f"DELETE FROM `{self.table_name}` WHERE id = %s;",
                        (account_id,),
                    )
                    conn.commit()
                    return True
                conn.commit()
                return False

    def restore_all(self, t: datetime) -> int:
        """整表還原：將整張主表還原至時間點 t 的狀態"""
        targets = self.query_at(t)
        target_map = {row["id"]: row for row in targets}

        conn = self.get_connection()
        affected_count = 0
        now_dt = datetime.now()

        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT id, name, balance FROM `{self.table_name}`;")
                current_rows = cur.fetchall()
                current_map = {row["id"]: row for row in current_rows}

                for acc_id, t_row in target_map.items():
                    if acc_id in current_map:
                        c_row = current_map[acc_id]
                        if c_row["name"] != t_row["name"] or c_row["balance"] != t_row["balance"]:
                            cur.execute(
                                f"""
                                UPDATE `{self.table_name}`
                                SET name = %s, balance = %s, updated_at = %s
                                WHERE id = %s;
                                """,
                                (t_row["name"], t_row["balance"], now_dt, acc_id),
                            )
                            affected_count += 1
                    else:
                        cur.execute(
                            f"""
                            INSERT INTO `{self.table_name}` (id, name, balance, updated_at)
                            VALUES (%s, %s, %s, %s);
                            """,
                            (t_row["id"], t_row["name"], t_row["balance"], now_dt),
                        )
                        affected_count += 1

                for acc_id in current_map:
                    if acc_id not in target_map:
                        cur.execute(
                            f"DELETE FROM `{self.table_name}` WHERE id = %s;",
                            (acc_id,),
                        )
                        affected_count += 1

            conn.commit()
            return affected_count
        except Exception:
            conn.rollback()
            raise

    def storage_size_mb(self) -> float:
        """計算原生系統版本化資料表的總佔用空間 (MB)"""
        conn = self.get_connection()
        db_name = conn.db.decode() if isinstance(conn.db, bytes) else str(conn.db)

        sql = """
            SELECT COALESCE(SUM(data_length + index_length), 0) / (1024 * 1024) AS size_mb
            FROM information_schema.TABLES
            WHERE table_schema = %s AND table_name = %s;
        """
        with conn.cursor() as cur:
            # 確保讀到最新統計資訊：執行 ANALYZE TABLE 更新統計資料
            cur.execute(f"ANALYZE TABLE `{self.table_name}`;")
            cur.execute(sql, (db_name, self.table_name))
            row = cur.fetchone()
            conn.commit()
            if row and row.get("size_mb") is not None:
                return float(row["size_mb"])
        return 0.0

    def get_history(self, account_id: int) -> List[Dict[str, Any]]:
        """
        使用 FOR SYSTEM_TIME ALL 查詢單一帳戶在底層引擎中的所有版本歷史
        包含系統自動賦予的 ROW_START 與 ROW_END
        """
        conn = self.get_connection()
        sql = f"""
            SELECT id, name, balance, updated_at,
                   ROW_START AS valid_from, ROW_END AS valid_to,
                   'native_version' AS record_type
            FROM `{self.table_name}` FOR SYSTEM_TIME ALL
            WHERE id = %s
            ORDER BY ROW_START ASC;
        """
        with conn.cursor() as cur:
            cur.execute(sql, (account_id,))
            rows = cur.fetchall()
        conn.commit()
        return rows

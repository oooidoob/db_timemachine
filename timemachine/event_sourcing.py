"""
設計 C：事件溯源 (Event Sourcing) 實作模組
所有變更皆以不可變的領域事件 (Append-only Events) 儲存。
支援檢查點快照 (Checkpoints)，透過重播 (Replay) 演算特定時間點的帳戶狀態。
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Any, Optional
import pymysql

from .base import BaseTimeMachine
from .db import get_connection


class EventSourcingTimeMachine(BaseTimeMachine):
    """
    設計 C：事件溯源時光機實作
    - 事件表 (account_events): 記錄不可變變更事件 (event_id, account_id, event_type, amount, new_name, occurred_at)
    - 檢查點表 (account_checkpoints): 週期性儲存狀態 (checkpoint_id, last_event_id, account_id, name, balance, created_at)
    - 投影主表 (accounts_c): 當前最新狀態物化視圖，加速當前讀取與差值計算
    """

    def __init__(
        self,
        events_table: str = "account_events",
        checkpoints_table: str = "account_checkpoints",
        main_table: str = "accounts_c",
        checkpoint_interval: int = 50,
        use_checkpoints: bool = True,
        db_type: str = "mysql",
    ):
        super().__init__(db_type=db_type)
        self.events_table = events_table
        self.checkpoints_table = checkpoints_table
        self.main_table = main_table
        self.checkpoint_interval = checkpoint_interval
        self.use_checkpoints = use_checkpoints
        self._op_counter = 0

    def get_connection(self) -> pymysql.Connection:
        """取得或建立 MySQL 連線"""
        if self.conn is None or not self.conn.open:
            self.conn = get_connection(self.db_type, autocommit=False)
        return self.conn

    def reset_data(self) -> None:
        """清空事件表、檢查點表與投影主表"""
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE `{self.checkpoints_table}`;")
            cur.execute(f"TRUNCATE TABLE `{self.events_table}`;")
            cur.execute(f"TRUNCATE TABLE `{self.main_table}`;")
        conn.commit()
        self._op_counter = 0

    def _maybe_checkpoint(self, account_id: int, now_dt: datetime) -> None:
        """根據操作次數判斷是否觸發檢查點儲存"""
        if not self.use_checkpoints:
            return

        self._op_counter += 1
        if self._op_counter >= self.checkpoint_interval:
            self._op_counter = 0
            self.create_checkpoint(account_id, now_dt)

    def create_checkpoint(self, account_id: int, timestamp: Optional[datetime] = None) -> None:
        """為指定帳戶建立當前狀態的檢查點"""
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()

        with conn.cursor() as cur:
            # 取得投影主表的最新狀態
            cur.execute(
                f"SELECT id, name, balance FROM `{self.main_table}` WHERE id = %s;",
                (account_id,),
            )
            acc = cur.fetchone()
            if not acc:
                conn.commit()
                return

            # 取得該帳戶最新的 event_id
            cur.execute(
                f"SELECT COALESCE(MAX(event_id), 0) AS max_id FROM `{self.events_table}` WHERE account_id = %s;",
                (account_id,),
            )
            max_ev = cur.fetchone()
            last_event_id = max_ev["max_id"] if max_ev else 0

            # 寫入檢查點表
            insert_chk = f"""
                INSERT INTO `{self.checkpoints_table}` (last_event_id, account_id, name, balance, created_at)
                VALUES (%s, %s, %s, %s, %s);
            """
            cur.execute(insert_chk, (last_event_id, account_id, acc["name"], acc["balance"], now_dt))
        conn.commit()

    def apply_insert(
        self,
        account_id: int,
        name: str,
        balance: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """寫入 CREATE 事件，並更新投影主表"""
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()

        with conn.cursor() as cur:
            # 寫入 CREATE 事件
            ev_sql = f"""
                INSERT INTO `{self.events_table}` (account_id, event_type, amount, new_name, occurred_at)
                VALUES (%s, 'CREATE', %s, %s, %s);
            """
            cur.execute(ev_sql, (account_id, balance, name, now_dt))

            # 更新投影表
            proj_sql = f"""
                INSERT INTO `{self.main_table}` (id, name, balance, updated_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE name = VALUES(name), balance = VALUES(balance), updated_at = VALUES(updated_at);
            """
            cur.execute(proj_sql, (account_id, name, balance, now_dt))
        conn.commit()

        self._maybe_checkpoint(account_id, now_dt)

    def apply_update(
        self,
        account_id: int,
        new_balance: int,
        new_name: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """
        更新帳戶：比對當前狀態，轉換為具體領域事件 (DEPOSIT, WITHDRAW, RENAME)。
        """
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()

        with conn.cursor() as cur:
            # 讀取當前投影值
            cur.execute(
                f"SELECT name, balance FROM `{self.main_table}` WHERE id = %s;",
                (account_id,),
            )
            curr = cur.fetchone()
            if not curr:
                # 若投影表無紀錄則視同初次建立
                curr_name = new_name or f"User{account_id}"
                curr_balance = 0
            else:
                curr_name = curr["name"]
                curr_balance = curr["balance"]

            # 處理名稱變更事件
            target_name = new_name if new_name is not None else curr_name
            if target_name != curr_name:
                cur.execute(
                    f"""
                    INSERT INTO `{self.events_table}` (account_id, event_type, amount, new_name, occurred_at)
                    VALUES (%s, 'RENAME', NULL, %s, %s);
                    """,
                    (account_id, target_name, now_dt),
                )

            # 處理餘額增減事件
            diff = new_balance - curr_balance
            if diff > 0:
                cur.execute(
                    f"""
                    INSERT INTO `{self.events_table}` (account_id, event_type, amount, new_name, occurred_at)
                    VALUES (%s, 'DEPOSIT', %s, NULL, %s);
                    """,
                    (account_id, diff, now_dt),
                )
            elif diff < 0:
                cur.execute(
                    f"""
                    INSERT INTO `{self.events_table}` (account_id, event_type, amount, new_name, occurred_at)
                    VALUES (%s, 'WITHDRAW', %s, NULL, %s);
                    """,
                    (account_id, abs(diff), now_dt),
                )

            # 同步更新投影主表
            cur.execute(
                f"""
                INSERT INTO `{self.main_table}` (id, name, balance, updated_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE name = VALUES(name), balance = VALUES(balance), updated_at = VALUES(updated_at);
                """,
                (account_id, target_name, new_balance, now_dt),
            )
        conn.commit()

        self._maybe_checkpoint(account_id, now_dt)

    def apply_delete(
        self,
        account_id: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """寫入 DELETE 事件，並從投影主表中移除"""
        conn = self.get_connection()
        now_dt = timestamp or datetime.now()

        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO `{self.events_table}` (account_id, event_type, amount, new_name, occurred_at)
                VALUES (%s, 'DELETE', NULL, NULL, %s);
                """,
                (account_id, now_dt),
            )
            cur.execute(f"DELETE FROM `{self.main_table}` WHERE id = %s;", (account_id,))
        conn.commit()

        self._maybe_checkpoint(account_id, now_dt)

    def state_at(self, t: datetime, account_id: Optional[int] = None) -> Dict[int, Dict[str, Any]]:
        """
        核心重播函式：演算時間點 t 當下的帳戶狀態。
        若啟動檢查點，則優先取 <= t 的最近一次檢查點，再重播後續事件。
        """
        conn = self.get_connection()
        states: Dict[int, Dict[str, Any]] = {}

        with conn.cursor() as cur:
            if account_id is not None:
                # --- 單筆帳戶重播 ---
                last_event_id = 0
                if self.use_checkpoints:
                    chk_sql = f"""
                        SELECT last_event_id, name, balance, created_at
                        FROM `{self.checkpoints_table}`
                        WHERE account_id = %s AND created_at <= %s
                        ORDER BY created_at DESC, checkpoint_id DESC
                        LIMIT 1;
                    """
                    cur.execute(chk_sql, (account_id, t))
                    chk = cur.fetchone()
                    if chk:
                        last_event_id = chk["last_event_id"]
                        states[account_id] = {
                            "id": account_id,
                            "name": chk["name"],
                            "balance": chk["balance"],
                            "updated_at": chk["created_at"],
                        }

                # 撈取該檢查點之後且 <= t 的事件
                ev_sql = f"""
                    SELECT event_id, event_type, amount, new_name, occurred_at
                    FROM `{self.events_table}`
                    WHERE account_id = %s AND event_id > %s AND occurred_at <= %s
                    ORDER BY occurred_at ASC, event_id ASC;
                """
                cur.execute(ev_sql, (account_id, last_event_id, t))
                events = cur.fetchall()

                curr = states.get(account_id)
                for ev in events:
                    etype = ev["event_type"]
                    if etype == "CREATE":
                        curr = {
                            "id": account_id,
                            "name": ev["new_name"],
                            "balance": ev["amount"] or 0,
                            "updated_at": ev["occurred_at"],
                        }
                    elif curr is not None:
                        if etype == "DEPOSIT":
                            curr["balance"] += ev["amount"] or 0
                            curr["updated_at"] = ev["occurred_at"]
                        elif etype == "WITHDRAW":
                            curr["balance"] -= ev["amount"] or 0
                            curr["updated_at"] = ev["occurred_at"]
                        elif etype == "RENAME":
                            curr["name"] = ev["new_name"]
                            curr["updated_at"] = ev["occurred_at"]
                        elif etype == "DELETE":
                            curr = None

                if curr is not None:
                    states[account_id] = curr
                else:
                    states.pop(account_id, None)

            else:
                # --- 全表重播 ---
                chk_map = {}
                if self.use_checkpoints:
                    # 取得每個帳戶在 <= t 的最新檢查點
                    chk_sql = f"""
                        SELECT c1.account_id, c1.last_event_id, c1.name, c1.balance, c1.created_at
                        FROM `{self.checkpoints_table}` c1
                        INNER JOIN (
                            SELECT account_id, MAX(checkpoint_id) AS max_chk_id
                            FROM `{self.checkpoints_table}`
                            WHERE created_at <= %s
                            GROUP BY account_id
                        ) c2 ON c1.checkpoint_id = c2.max_chk_id;
                    """
                    cur.execute(chk_sql, (t,))
                    checkpoints = cur.fetchall()
                    for chk in checkpoints:
                        aid = chk["account_id"]
                        chk_map[aid] = chk["last_event_id"]
                        states[aid] = {
                            "id": aid,
                            "name": chk["name"],
                            "balance": chk["balance"],
                            "updated_at": chk["created_at"],
                        }

                # 撈取 <= t 的所有事件，依時間排序逐批分段重播（避免大量事件造成記憶體過載）
                ev_sql = f"""
                    SELECT event_id, account_id, event_type, amount, new_name, occurred_at
                    FROM `{self.events_table}`
                    WHERE occurred_at <= %s
                    ORDER BY occurred_at ASC, event_id ASC;
                """
                cur.execute(ev_sql, (t,))
                batch_size = 5000
                while True:
                    batch = cur.fetchmany(batch_size)
                    if not batch:
                        break

                    for ev in batch:
                        aid = ev["account_id"]
                        # 若已有檢查點且事件在檢查點之前，略過該事件
                        if aid in chk_map and ev["event_id"] <= chk_map[aid]:
                            continue

                        etype = ev["event_type"]
                        curr = states.get(aid)

                        if etype == "CREATE":
                            states[aid] = {
                                "id": aid,
                                "name": ev["new_name"],
                                "balance": ev["amount"] or 0,
                                "updated_at": ev["occurred_at"],
                            }
                        elif curr is not None:
                            if etype == "DEPOSIT":
                                curr["balance"] += ev["amount"] or 0
                                curr["updated_at"] = ev["occurred_at"]
                            elif etype == "WITHDRAW":
                                curr["balance"] -= ev["amount"] or 0
                                curr["updated_at"] = ev["occurred_at"]
                            elif etype == "RENAME":
                                curr["name"] = ev["new_name"]
                                curr["updated_at"] = ev["occurred_at"]
                            elif etype == "DELETE":
                                states.pop(aid, None)

        conn.commit()
        return states

    def query_at(self, t: datetime) -> List[Dict[str, Any]]:
        """全表時間點查詢：透過重播計算時間點 t 的所有存活帳戶"""
        state_dict = self.state_at(t, account_id=None)
        rows = list(state_dict.values())
        rows.sort(key=lambda x: x["id"])
        return rows

    def query_one_at(self, account_id: int, t: datetime) -> Optional[Dict[str, Any]]:
        """單筆時間點查詢：透過重播計算時間點 t 該帳戶的狀態"""
        state_dict = self.state_at(t, account_id=account_id)
        return state_dict.get(account_id)

    def restore_one(self, account_id: int, t: datetime) -> bool:
        """單筆還原：計算時間點 t 狀態，並產生相應的新事件以回復至該狀態"""
        target = self.query_one_at(account_id, t)
        curr = self.query_one_at(account_id, datetime.now())

        if target is not None:
            if curr is not None:
                if curr["name"] == target["name"] and curr["balance"] == target["balance"]:
                    return False
                self.apply_update(account_id, new_balance=target["balance"], new_name=target["name"])
                return True
            else:
                self.apply_insert(account_id, name=target["name"], balance=target["balance"])
                return True
        else:
            if curr is not None:
                self.apply_delete(account_id)
                return True
            return False

    def restore_all(self, t: datetime) -> int:
        """整表還原：將整張表還原至時間點 t 的狀態"""
        target_map = self.state_at(t, account_id=None)
        current_map = self.state_at(datetime.now(), account_id=None)

        affected = 0
        for aid, t_row in target_map.items():
            if aid in current_map:
                c_row = current_map[aid]
                if c_row["name"] != t_row["name"] or c_row["balance"] != t_row["balance"]:
                    self.apply_update(aid, new_balance=t_row["balance"], new_name=t_row["name"])
                    affected += 1
            else:
                self.apply_insert(aid, name=t_row["name"], balance=t_row["balance"])
                affected += 1

        for aid in current_map:
            if aid not in target_map:
                self.apply_delete(aid)
                affected += 1

        return affected

    def storage_size_mb(self) -> float:
        """計算設計 C（事件表 ＋ 檢查點表 ＋ 投影表）的總佔用空間 (MB)"""
        conn = self.get_connection()
        db_name = conn.db.decode() if isinstance(conn.db, bytes) else str(conn.db)

        sql = """
            SELECT COALESCE(SUM(data_length + index_length), 0) / (1024 * 1024) AS size_mb
            FROM information_schema.TABLES
            WHERE table_schema = %s AND table_name IN (%s, %s, %s);
        """
        with conn.cursor() as cur:
            # 確保讀到最新統計資訊：關閉 MySQL 8 統計快取並執行 ANALYZE TABLE
            try:
                cur.execute("SET SESSION information_schema_stats_expiry = 0;")
            except Exception:
                pass
            cur.execute(f"ANALYZE TABLE `{self.events_table}`, `{self.checkpoints_table}`, `{self.main_table}`;")
            cur.execute(sql, (db_name, self.events_table, self.checkpoints_table, self.main_table))
            row = cur.fetchone()
            conn.commit()
            if row and row.get("size_mb") is not None:
                return float(row["size_mb"])
        return 0.0

    def get_history(self, account_id: int) -> List[Dict[str, Any]]:
        """取得單一帳戶的所有歷史事件時間軸"""
        conn = self.get_connection()
        sql = f"""
            SELECT event_id, account_id, event_type, amount, new_name, occurred_at AS valid_from, 'event' AS record_type
            FROM `{self.events_table}`
            WHERE account_id = %s
            ORDER BY occurred_at ASC, event_id ASC;
        """
        with conn.cursor() as cur:
            cur.execute(sql, (account_id,))
            rows = cur.fetchall()
        conn.commit()
        return rows

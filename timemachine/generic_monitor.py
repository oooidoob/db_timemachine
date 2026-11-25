"""
通用即時監控與時光機掛載模組 (generic_monitor.py)
支援動態探索任意 MySQL 8 / MariaDB 11 資料庫與任意自訂結構資料表，
並透過「JSON 審計觸發器 (MySQL)」或「原生系統版本化 (MariaDB)」提供即時監控、時間點查詢與無損還原。
"""

from __future__ import annotations
import json
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import pymysql
from pymysql.cursors import DictCursor


SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}
HISTORY_SUFFIX = "_tm_history"


def _quote_ident(name: str) -> str:
    """安全跳脫 SQL 識別字（資料庫名、表名、欄位名）"""
    return "`" + name.replace("`", "``") + "`"


def _local_tz_offset() -> str:
    """取得本機時區偏移字串，例如 '+08:00'"""
    local_tz = datetime.now().astimezone().strftime("%z")
    return f"{local_tz[:3]}:{local_tz[3:]}" if len(local_tz) == 5 else "+00:00"


def connect_custom(
    host: str,
    port: int,
    user: str,
    password: str,
    db_type: str = "mysql",
) -> pymysql.Connection:
    """建立自訂連線參數的資料庫連線，並同步本機時區"""
    try:
        conn = pymysql.connect(
            host=host,
            port=int(port),
            user=user,
            password=password,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=5,
        )
        tz_str = _local_tz_offset()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET time_zone = '{tz_str}';")
        except Exception:
            pass
        return conn
    except pymysql.err.OperationalError as e:
        code = e.args[0]
        msg = e.args[1] if len(e.args) > 1 else str(e)
        if code == 2003:
            raise ConnectionError(f"無法連線至 {db_type.upper()} ({host}:{port})，請確認資料庫已啟動且埠號開放。") from e
        elif code == 1045:
            raise ConnectionError(f"連線至 {db_type.upper()} 失敗：帳號或密碼錯誤。") from e
        raise ConnectionError(f"連線錯誤 ({code})：{msg}") from e


class GenericMonitor:
    """通用資料庫即時監控與時光機管理器"""

    @staticmethod
    def list_databases(conn: pymysql.Connection) -> List[str]:
        """列出伺服器上所有非系統資料庫"""
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES;")
            rows = cur.fetchall()
        conn.commit()
        dbs = []
        for r in rows:
            db_name = next(iter(r.values()))
            if db_name.lower() not in SYSTEM_DATABASES:
                dbs.append(db_name)
        return sorted(dbs)

    @staticmethod
    def list_tables(conn: pymysql.Connection, database: str) -> List[str]:
        """列出指定資料庫中的所有使用者資料表（過濾內部的 _tm_history 影子表）"""
        q_db = _quote_ident(database)
        with conn.cursor() as cur:
            cur.execute(f"SHOW FULL TABLES FROM {q_db} WHERE Table_type = 'BASE TABLE';")
            rows = cur.fetchall()
        conn.commit()
        tables = []
        for r in rows:
            tbl_name = next(iter(r.values()))
            if not tbl_name.endswith(HISTORY_SUFFIX):
                tables.append(tbl_name)
        return sorted(tables)

    @staticmethod
    def get_table_schema(conn: pymysql.Connection, database: str, table: str) -> Dict[str, Any]:
        """取得資料表的所有欄位與主鍵資訊"""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, COLUMN_KEY
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION ASC;
                """,
                (database, table),
            )
            cols = cur.fetchall()

            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY'
                ORDER BY ORDINAL_POSITION ASC;
                """,
                (database, table),
            )
            pk_rows = cur.fetchall()
        conn.commit()

        columns = [c["COLUMN_NAME"] for c in cols]
        pk_cols = [p["COLUMN_NAME"] for p in pk_rows]
        pk_col = pk_cols[0] if len(pk_cols) == 1 else None
        return {
            "columns": columns,
            "pk_cols": pk_cols,
            "pk_col": pk_col,
        }

    @staticmethod
    def is_tracking_enabled(conn: pymysql.Connection, database: str, table: str, db_type: str) -> bool:
        """檢查指定資料表是否已啟用時光機追蹤"""
        q_db = _quote_ident(database)
        q_tbl = _quote_ident(table)

        with conn.cursor() as cur:
            if db_type == "mariadb":
                cur.execute(f"SHOW CREATE TABLE {q_db}.{q_tbl};")
                row = cur.fetchone()
                conn.commit()
                if row:
                    create_sql = row.get("Create Table", "")
                    return "WITH SYSTEM VERSIONING" in create_sql.upper()
                return False
            else:
                hist_tbl = f"{table}{HISTORY_SUFFIX}"
                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s;
                    """,
                    (database, hist_tbl),
                )
                has_hist = cur.fetchone()["cnt"] > 0

                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM information_schema.TRIGGERS
                    WHERE TRIGGER_SCHEMA = %s AND EVENT_OBJECT_TABLE = %s
                      AND TRIGGER_NAME LIKE %s;
                    """,
                    (database, table, f"trg_tm_{table}_%"),
                )
                trig_cnt = cur.fetchone()["cnt"]
                conn.commit()
                return has_hist and trig_cnt >= 3

    @staticmethod
    def enable_tracking(conn: pymysql.Connection, database: str, table: str, db_type: str) -> int:
        """
        一鍵為任意資料表掛載時光機追蹤：
        - MariaDB：執行 ALTER TABLE ... ADD SYSTEM VERSIONING
        - MySQL：建立 {table}_tm_history 影子表、匯入初始快照、並安裝 INSERT/UPDATE/DELETE JSON 觸發器
        回傳初始化寫入的快照筆數。
        """
        schema = GenericMonitor.get_table_schema(conn, database, table)
        columns = schema["columns"]
        pk_col = schema["pk_col"]

        if not columns:
            raise ValueError(f"資料表 `{database}.{table}` 沒有任何欄位。")

        q_db = _quote_ident(database)
        q_tbl = _quote_ident(table)

        if db_type == "mariadb":
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {q_db}.{q_tbl} ADD SYSTEM VERSIONING;")
            conn.commit()
            return 0

        # MySQL 模式需要單一主鍵以精確識別每筆資料
        if not pk_col:
            raise ValueError(
                f"資料表 `{database}.{table}` 必須具備「單一欄位主鍵 (Primary Key)」才能掛載 MySQL 通用時光機觸發器。"
            )

        hist_tbl = f"{table}{HISTORY_SUFFIX}"
        q_hist = _quote_ident(hist_tbl)
        q_pk = _quote_ident(pk_col)
        tz_str = _local_tz_offset()

        # 組合 JSON_OBJECT 欄位表達式
        old_json_pairs = ", ".join([f"'{c}', OLD.{_quote_ident(c)}" for c in columns])
        new_json_pairs = ", ".join([f"'{c}', NEW.{_quote_ident(c)}" for c in columns])
        cur_json_pairs = ", ".join([f"'{c}', {_quote_ident(c)}" for c in columns])

        # 觸發器內使用 CONVERT_TZ(UTC_TIMESTAMP(6), '+00:00', tz_str) 確保外部程式寫入也符合本機時區
        now_expr = f"CONVERT_TZ(UTC_TIMESTAMP(6), '+00:00', '{tz_str}')"

        trg_ins = _quote_ident(f"trg_tm_{table}_ins")
        trg_upd = _quote_ident(f"trg_tm_{table}_upd")
        trg_del = _quote_ident(f"trg_tm_{table}_del")

        init_count = 0
        now_dt = datetime.now()

        with conn.cursor() as cur:
            # 1. 建立通用歷史影子表
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {q_db}.{q_hist} (
                    history_id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    pk_value VARCHAR(255) NOT NULL,
                    op_type ENUM('INIT', 'INSERT', 'UPDATE', 'DELETE') NOT NULL,
                    old_data JSON DEFAULT NULL,
                    new_data JSON DEFAULT NULL,
                    changed_at DATETIME(6) NOT NULL,
                    INDEX idx_tm_hist_pk_time (pk_value, changed_at),
                    INDEX idx_tm_hist_time (changed_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )

            # 2. 若歷史表為空，先將目前主表既有資料拍攝一份 INIT 基線
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {q_db}.{q_hist};")
            if cur.fetchone()["cnt"] == 0:
                init_count = cur.execute(
                    f"""
                    INSERT INTO {q_db}.{q_hist} (pk_value, op_type, old_data, new_data, changed_at)
                    SELECT CAST({q_pk} AS CHAR(255)), 'INIT', NULL, JSON_OBJECT({cur_json_pairs}), %s
                    FROM {q_db}.{q_tbl};
                    """,
                    (now_dt,),
                )

            # 3. 清除舊觸發器並安裝三個即時監控觸發器
            cur.execute(f"DROP TRIGGER IF EXISTS {q_db}.{trg_ins};")
            cur.execute(
                f"""
                CREATE TRIGGER {q_db}.{trg_ins}
                AFTER INSERT ON {q_db}.{q_tbl}
                FOR EACH ROW
                BEGIN
                    INSERT INTO {q_db}.{q_hist} (pk_value, op_type, old_data, new_data, changed_at)
                    VALUES (CAST(NEW.{q_pk} AS CHAR(255)), 'INSERT', NULL, JSON_OBJECT({new_json_pairs}), {now_expr});
                END
                """
            )

            cur.execute(f"DROP TRIGGER IF EXISTS {q_db}.{trg_upd};")
            cur.execute(
                f"""
                CREATE TRIGGER {q_db}.{trg_upd}
                AFTER UPDATE ON {q_db}.{q_tbl}
                FOR EACH ROW
                BEGIN
                    INSERT INTO {q_db}.{q_hist} (pk_value, op_type, old_data, new_data, changed_at)
                    VALUES (CAST(NEW.{q_pk} AS CHAR(255)), 'UPDATE', JSON_OBJECT({old_json_pairs}), JSON_OBJECT({new_json_pairs}), {now_expr});
                END
                """
            )

            cur.execute(f"DROP TRIGGER IF EXISTS {q_db}.{trg_del};")
            cur.execute(
                f"""
                CREATE TRIGGER {q_db}.{trg_del}
                AFTER DELETE ON {q_db}.{q_tbl}
                FOR EACH ROW
                BEGIN
                    INSERT INTO {q_db}.{q_hist} (pk_value, op_type, old_data, new_data, changed_at)
                    VALUES (CAST(OLD.{q_pk} AS CHAR(255)), 'DELETE', JSON_OBJECT({old_json_pairs}), NULL, {now_expr});
                END
                """
            )
        conn.commit()
        return init_count

    @staticmethod
    def get_current_rows(conn: pymysql.Connection, database: str, table: str, limit: int = 500) -> List[Dict[str, Any]]:
        """讀取目前資料表的最新內容"""
        q_db = _quote_ident(database)
        q_tbl = _quote_ident(table)
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {q_db}.{q_tbl} LIMIT %s;", (limit,))
            rows = cur.fetchall()
        conn.commit()
        return rows

    @staticmethod
    def query_at(conn: pymysql.Connection, database: str, table: str, t: datetime, db_type: str) -> List[Dict[str, Any]]:
        """查詢任意資料表在時間點 t 的狀態"""
        q_db = _quote_ident(database)
        q_tbl = _quote_ident(table)

        if db_type == "mariadb":
            t_str = t.strftime("%Y-%m-%d %H:%M:%S.%f") if isinstance(t, datetime) else str(t)
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {q_db}.{q_tbl} FOR SYSTEM_TIME AS OF %s;", (t_str,))
                rows = cur.fetchall()
            conn.commit()
            return rows

        # MySQL：從 {table}_tm_history 分批重播至時間點 t
        hist_tbl = f"{table}{HISTORY_SUFFIX}"
        q_hist = _quote_ident(hist_tbl)
        states: Dict[str, Dict[str, Any]] = {}

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT pk_value, op_type, new_data
                FROM {q_db}.{q_hist}
                WHERE changed_at <= %s
                ORDER BY changed_at ASC, history_id ASC;
                """,
                (t,),
            )
            while True:
                batch = cur.fetchmany(5000)
                if not batch:
                    break
                for ev in batch:
                    pk_val = ev["pk_value"]
                    if ev["op_type"] == "DELETE":
                        states.pop(pk_val, None)
                    else:
                        data = ev["new_data"]
                        if isinstance(data, str):
                            data = json.loads(data)
                        if data is not None:
                            states[pk_val] = data
        conn.commit()
        return list(states.values())

    @staticmethod
    def get_row_history(
        conn: pymysql.Connection,
        database: str,
        table: str,
        pk_val: Any,
        db_type: str,
    ) -> List[Dict[str, Any]]:
        """取得單一主鍵資料列的完整變更時間軸"""
        schema = GenericMonitor.get_table_schema(conn, database, table)
        pk_col = schema["pk_col"]
        if not pk_col:
            return []

        q_db = _quote_ident(database)
        q_tbl = _quote_ident(table)
        q_pk = _quote_ident(pk_col)

        with conn.cursor() as cur:
            if db_type == "mariadb":
                cur.execute(
                    f"""
                    SELECT *, ROW_START AS valid_from, ROW_END AS valid_to
                    FROM {q_db}.{q_tbl} FOR SYSTEM_TIME ALL
                    WHERE {q_pk} = %s
                    ORDER BY ROW_START ASC;
                    """,
                    (pk_val,),
                )
                rows = cur.fetchall()
                conn.commit()
                return rows
            else:
                hist_tbl = f"{table}{HISTORY_SUFFIX}"
                q_hist = _quote_ident(hist_tbl)
                cur.execute(
                    f"""
                    SELECT history_id, pk_value, op_type, old_data, new_data, changed_at
                    FROM {q_db}.{q_hist}
                    WHERE pk_value = %s
                    ORDER BY changed_at ASC, history_id ASC;
                    """,
                    (str(pk_val),),
                )
                rows = cur.fetchall()
                conn.commit()
                return rows

    @staticmethod
    def _apply_row_sync(
        conn: pymysql.Connection,
        database: str,
        table: str,
        columns: List[str],
        pk_col: str,
        target_map: Dict[str, Dict[str, Any]],
        current_map: Dict[str, Dict[str, Any]],
        only_pk: Optional[str] = None,
    ) -> int:
        """動態產生參數化 SQL，同步還原單筆或全表資料"""
        q_db = _quote_ident(database)
        q_tbl = _quote_ident(table)
        q_pk = _quote_ident(pk_col)

        col_list_sql = ", ".join(_quote_ident(c) for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        non_pk_cols = [c for c in columns if c != pk_col]

        affected = 0
        keys_to_process = [only_pk] if only_pk is not None else sorted(set(target_map.keys()) | set(current_map.keys()))

        try:
            with conn.cursor() as cur:
                for pk_str in keys_to_process:
                    t_row = target_map.get(pk_str)
                    c_row = current_map.get(pk_str)

                    if t_row is not None:
                        if c_row is not None:
                            # 比對是否有差異
                            changed = any(str(c_row.get(c)) != str(t_row.get(c)) for c in columns)
                            if changed and non_pk_cols:
                                set_clause = ", ".join(f"{_quote_ident(c)} = %s" for c in non_pk_cols)
                                vals = [t_row.get(c) for c in non_pk_cols] + [t_row.get(pk_col)]
                                cur.execute(f"UPDATE {q_db}.{q_tbl} SET {set_clause} WHERE {q_pk} = %s;", vals)
                                affected += 1
                        else:
                            vals = [t_row.get(c) for c in columns]
                            cur.execute(f"INSERT INTO {q_db}.{q_tbl} ({col_list_sql}) VALUES ({placeholders});", vals)
                            affected += 1
                    else:
                        if c_row is not None:
                            cur.execute(f"DELETE FROM {q_db}.{q_tbl} WHERE {q_pk} = %s;", (c_row.get(pk_col),))
                            affected += 1
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def restore_one(conn: pymysql.Connection, database: str, table: str, pk_val: Any, t: datetime, db_type: str) -> bool:
        """將任意資料表的單筆主鍵資料還原至時間點 t"""
        schema = GenericMonitor.get_table_schema(conn, database, table)
        pk_col = schema["pk_col"]
        if not pk_col:
            raise ValueError("資料表缺少單一主鍵，無法執行單筆還原。")

        target_rows = GenericMonitor.query_at(conn, database, table, t, db_type)
        current_rows = GenericMonitor.get_current_rows(conn, database, table, limit=100000)

        target_map = {str(r[pk_col]): r for r in target_rows}
        current_map = {str(r[pk_col]): r for r in current_rows}

        affected = GenericMonitor._apply_row_sync(
            conn, database, table, schema["columns"], pk_col, target_map, current_map, only_pk=str(pk_val)
        )
        return affected > 0

    @staticmethod
    def restore_all(conn: pymysql.Connection, database: str, table: str, t: datetime, db_type: str) -> int:
        """將任意資料表的整表內容還原至時間點 t"""
        schema = GenericMonitor.get_table_schema(conn, database, table)
        pk_col = schema["pk_col"]
        if not pk_col:
            raise ValueError("資料表缺少單一主鍵，無法執行整表還原。")

        target_rows = GenericMonitor.query_at(conn, database, table, t, db_type)
        current_rows = GenericMonitor.get_current_rows(conn, database, table, limit=100000)

        target_map = {str(r[pk_col]): r for r in target_rows}
        current_map = {str(r[pk_col]): r for r in current_rows}

        return GenericMonitor._apply_row_sync(
            conn, database, table, schema["columns"], pk_col, target_map, current_map, only_pk=None
        )

    @staticmethod
    def storage_size_mb(conn: pymysql.Connection, database: str, table: str, db_type: str) -> float:
        """計算資料表（含其歷史影子表）的總佔用空間 (MB)"""
        hist_tbl = f"{table}{HISTORY_SUFFIX}"
        q_db = _quote_ident(database)
        q_tbl = _quote_ident(table)
        q_hist = _quote_ident(hist_tbl)

        with conn.cursor() as cur:
            if db_type == "mysql":
                try:
                    cur.execute("SET SESSION information_schema_stats_expiry = 0;")
                except Exception:
                    pass
                cur.execute(f"ANALYZE TABLE {q_db}.{q_tbl};")
                try:
                    cur.execute(f"ANALYZE TABLE {q_db}.{q_hist};")
                except Exception:
                    pass
                cur.execute(
                    """
                    SELECT COALESCE(SUM(data_length + index_length), 0) / (1024 * 1024) AS size_mb
                    FROM information_schema.TABLES
                    WHERE table_schema = %s AND table_name IN (%s, %s);
                    """,
                    (database, table, hist_tbl),
                )
            else:
                cur.execute(f"ANALYZE TABLE {q_db}.{q_tbl};")
                cur.execute(
                    """
                    SELECT COALESCE(SUM(data_length + index_length), 0) / (1024 * 1024) AS size_mb
                    FROM information_schema.TABLES
                    WHERE table_schema = %s AND table_name = %s;
                    """,
                    (database, table),
                )
            row = cur.fetchone()
        conn.commit()
        return float(row["size_mb"]) if row and row.get("size_mb") is not None else 0.0

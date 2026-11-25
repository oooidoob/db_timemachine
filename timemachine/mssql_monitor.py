"""
Microsoft SQL Server (MSSQL) 通用即時監控與時光機模組 (mssql_monitor.py)
支援透過 pyodbc 連接本機或遠端 SQL Server（支援 Windows 驗證與 SQL 帳號驗證），
並利用 SQL Server 2016+ 原生時態資料表 (SYSTEM_VERSIONING = ON) 實現任意資料表的時間旅行與還原。
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

try:
    import pyodbc
    HAS_PYODBC = True
except ImportError:
    pyodbc = None
    HAS_PYODBC = False


def _quote_bracket(name: str) -> str:
    """安全跳脫 T-SQL 方括號識別字"""
    return "[" + name.replace("]", "]]") + "]"


def _split_schema_table(full_table: str) -> Tuple[str, str]:
    """拆解 'dbo.Orders' 為 ('dbo', 'Orders')"""
    if "." in full_table:
        schema, table = full_table.split(".", 1)
        return schema, table
    return "dbo", full_table


def _local_to_utc(dt: datetime) -> datetime:
    """將本機時間轉為 SQL Server SYSTEM_TIME 使用的 UTC 時間"""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_to_local(dt: Any) -> Any:
    """將 SQL Server 回傳的 UTC 時間轉回本機時間顯示（9999 年代表目前仍有效，回傳 None）"""
    if not isinstance(dt, datetime):
        return dt
    if dt.year >= 9999:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return dt


def _rows_to_dicts(cursor) -> List[Dict[str, Any]]:
    """將 pyodbc 查詢結果轉換為與 PyMySQL DictCursor 相同的字典列表"""
    if not cursor.description:
        return []
    cols = [col[0] for col in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _pick_odbc_driver() -> str:
    """自動挑選本機已安裝的最佳 SQL Server ODBC 驅動程式"""
    if not HAS_PYODBC:
        raise ImportError(
            "【缺少必要套件】未安裝 pyodbc 套件！\n"
            "請在終端機執行：pip install pyodbc"
        )
    available = pyodbc.drivers()
    preferred = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
    ]
    for drv in preferred:
        if drv in available:
            return drv
    for drv in available:
        if "SQL Server" in drv:
            return drv
    raise ConnectionError(
        "找不到 SQL Server 的 ODBC 驅動程式！請確認已安裝 SSMS 或 Microsoft ODBC Driver for SQL Server。"
    )


def connect_mssql(
    server: str = "localhost",
    trusted_connection: bool = True,
    user: str = "",
    password: str = "",
    database: Optional[str] = None,
):
    """
    建立 Microsoft SQL Server 連線。
    預設使用 Windows 驗證 (Trusted_Connection=yes) 連線至 localhost。
    """
    driver = _pick_odbc_driver()
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        "TrustServerCertificate=yes",
    ]
    if database:
        parts.append(f"DATABASE={database}")
    if trusted_connection:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={user}")
        parts.append(f"PWD={password}")

    conn_str = ";".join(parts) + ";"
    try:
        conn = pyodbc.connect(conn_str, timeout=5, autocommit=False)
        return conn
    except Exception as e:
        raise ConnectionError(
            f"無法連線至 SQL Server ({server})！\n"
            f"【可能原因】SQL Server 服務未啟動、伺服器名稱錯誤（例如 SQLEXPRESS 需填 localhost\\SQLEXPRESS）或驗證失敗。\n"
            f"【原始錯誤】{e}"
        ) from e


class MssqlMonitor:
    """Microsoft SQL Server 通用即時監控與時態還原管理器"""

    @staticmethod
    def list_databases(conn) -> List[str]:
        """列出使用者自建的線上資料庫（排除 master/tempdb/model/msdb）"""
        cur = conn.cursor()
        cur.execute(
            """
            SELECT name
            FROM sys.databases
            WHERE database_id > 4 AND state_desc = 'ONLINE'
            ORDER BY name;
            """
        )
        rows = [r[0] for r in cur.fetchall()]
        conn.commit()
        return rows

    @staticmethod
    def list_tables(conn, database: str) -> List[str]:
        """列出指定資料庫中的所有使用者資料表（排除時態歷史表與 _tm_history）"""
        q_db = _quote_bracket(database)
        cur = conn.cursor()
        cur.execute(f"USE {q_db};")
        cur.execute(
            """
            SELECT s.name AS schema_name, t.name AS table_name
            FROM sys.tables t
            JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE t.temporal_type != 1
              AND t.is_ms_shipped = 0
              AND t.name NOT LIKE '%_tm_history'
            ORDER BY s.name, t.name;
            """
        )
        rows = [f"{r[0]}.{r[1]}" for r in cur.fetchall()]
        conn.commit()
        return rows

    @staticmethod
    def get_table_schema(conn, database: str, full_table: str) -> Dict[str, Any]:
        """取得資料表的可寫入一般欄位、主鍵與 IDENTITY 資訊"""
        schema, table = _split_schema_table(full_table)
        q_db = _quote_bracket(database)
        cur = conn.cursor()
        cur.execute(f"USE {q_db};")
        cur.execute(
            """
            SELECT
                c.name AS column_name,
                c.is_identity,
                c.is_computed,
                c.generated_always_type
            FROM sys.columns c
            JOIN sys.tables t ON c.object_id = t.object_id
            JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE s.name = ? AND t.name = ?
            ORDER BY c.column_id ASC;
            """,
            (schema, table),
        )
        col_rows = _rows_to_dicts(cur)

        cur.execute(
            """
            SELECT col.name AS pk_col
            FROM sys.indexes i
            JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            JOIN sys.columns col ON ic.object_id = col.object_id AND ic.column_id = col.column_id
            JOIN sys.tables t ON i.object_id = t.object_id
            JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE i.is_primary_key = 1 AND s.name = ? AND t.name = ?
            ORDER BY ic.key_ordinal ASC;
            """,
            (schema, table),
        )
        pk_rows = [r[0] for r in cur.fetchall()]
        conn.commit()

        # 排除系統產生的隱藏期間欄位與計算欄位
        columns = [
            c["column_name"]
            for c in col_rows
            if not c["is_computed"] and c["generated_always_type"] == 0
        ]
        identity_cols = [c["column_name"] for c in col_rows if c["is_identity"]]
        pk_col = pk_rows[0] if len(pk_rows) == 1 else None

        return {
            "schema": schema,
            "table": table,
            "columns": columns,
            "pk_cols": pk_rows,
            "pk_col": pk_col,
            "has_identity": len(identity_cols) > 0,
        }

    @staticmethod
    def is_tracking_enabled(conn, database: str, full_table: str) -> bool:
        """檢查資料表是否已啟用 SQL Server 原生 Temporal Table (SYSTEM_VERSIONING = ON)"""
        schema, table = _split_schema_table(full_table)
        q_db = _quote_bracket(database)
        cur = conn.cursor()
        cur.execute(f"USE {q_db};")
        cur.execute(
            """
            SELECT t.temporal_type
            FROM sys.tables t
            JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE s.name = ? AND t.name = ?;
            """,
            (schema, table),
        )
        row = cur.fetchone()
        conn.commit()
        return bool(row and row[0] == 2)

    @staticmethod
    def enable_tracking(conn, database: str, full_table: str) -> None:
        """一鍵為既有 SQL Server 資料表啟用原生的 Temporal Table (SYSTEM_VERSIONING = ON)"""
        info = MssqlMonitor.get_table_schema(conn, database, full_table)
        if not info["pk_col"]:
            raise ValueError(
                f"資料表 `{full_table}` 必須具備「單一欄位主鍵 (Primary Key)」才能啟用 SQL Server 時態版本化。"
            )

        schema, table = info["schema"], info["table"]
        q_db = _quote_bracket(database)
        q_schema = _quote_bracket(schema)
        q_table = _quote_bracket(table)
        q_hist = _quote_bracket(f"{table}_tm_history")

        # 避免與現有欄位衝突
        existing = set(c.lower() for c in info["columns"])
        vf_col = "ValidFrom" if "validfrom" not in existing else "TM_ValidFrom"
        vt_col = "ValidTo" if "validto" not in existing else "TM_ValidTo"

        cur = conn.cursor()
        cur.execute(f"USE {q_db};")
        cur.execute(
            f"""
            ALTER TABLE {q_schema}.{q_table}
            ADD {_quote_bracket(vf_col)} DATETIME2 GENERATED ALWAYS AS ROW START HIDDEN NOT NULL
                    CONSTRAINT {_quote_bracket(f'DF_{table}_{vf_col}')} DEFAULT SYSUTCDATETIME(),
                {_quote_bracket(vt_col)} DATETIME2 GENERATED ALWAYS AS ROW END HIDDEN NOT NULL
                    CONSTRAINT {_quote_bracket(f'DF_{table}_{vt_col}')} DEFAULT CONVERT(DATETIME2, '9999-12-31 23:59:59.9999999'),
                PERIOD FOR SYSTEM_TIME ({_quote_bracket(vf_col)}, {_quote_bracket(vt_col)});
            """
        )
        cur.execute(
            f"""
            ALTER TABLE {q_schema}.{q_table}
            SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = {q_schema}.{q_hist}));
            """
        )
        conn.commit()

    @staticmethod
    def get_current_rows(conn, database: str, full_table: str, limit: int = 500) -> List[Dict[str, Any]]:
        """讀取目前資料表的最新內容"""
        schema, table = _split_schema_table(full_table)
        q_db = _quote_bracket(database)
        q_schema = _quote_bracket(schema)
        q_table = _quote_bracket(table)

        cur = conn.cursor()
        cur.execute(f"USE {q_db};")
        cur.execute(f"SELECT TOP ({int(limit)}) * FROM {q_schema}.{q_table};")
        rows = _rows_to_dicts(cur)
        conn.commit()
        return rows

    @staticmethod
    def query_at(conn, database: str, full_table: str, t: datetime) -> List[Dict[str, Any]]:
        """使用 FOR SYSTEM_TIME AS OF 查詢任意時間點 (自動將本機時間轉為 UTC)"""
        schema, table = _split_schema_table(full_table)
        q_db = _quote_bracket(database)
        q_schema = _quote_bracket(schema)
        q_table = _quote_bracket(table)

        t_utc = _local_to_utc(t)

        cur = conn.cursor()
        cur.execute(f"USE {q_db};")
        cur.execute(
            f"SELECT * FROM {q_schema}.{q_table} FOR SYSTEM_TIME AS OF ?;",
            (t_utc,),
        )
        rows = _rows_to_dicts(cur)
        conn.commit()
        return rows

    @staticmethod
    def get_row_history(conn, database: str, full_table: str, pk_val: Any) -> List[Dict[str, Any]]:
        """取得單一主鍵資料列的完整變更時間軸（並將 UTC 轉回本機時間顯示）"""
        info = MssqlMonitor.get_table_schema(conn, database, full_table)
        pk_col = info["pk_col"]
        if not pk_col:
            return []

        schema, table = info["schema"], info["table"]
        q_db = _quote_bracket(database)
        q_schema = _quote_bracket(schema)
        q_table = _quote_bracket(table)
        q_pk = _quote_bracket(pk_col)

        cur = conn.cursor()
        cur.execute(f"USE {q_db};")
        # 動態查出該表 SYSTEM_TIME 的起訖欄位名稱
        cur.execute(
            """
            SELECT cs.name AS start_col, ce.name AS end_col
            FROM sys.periods p
            JOIN sys.tables t ON p.object_id = t.object_id
            JOIN sys.schemas s ON t.schema_id = s.schema_id
            JOIN sys.columns cs ON p.object_id = cs.object_id AND p.start_column_id = cs.column_id
            JOIN sys.columns ce ON p.object_id = ce.object_id AND p.end_column_id = ce.column_id
            WHERE s.name = ? AND t.name = ?;
            """,
            (schema, table),
        )
        period_row = cur.fetchone()
        if not period_row:
            conn.commit()
            return []

        start_col, end_col = period_row[0], period_row[1]
        cur.execute(
            f"""
            SELECT *, {_quote_bracket(start_col)} AS valid_from, {_quote_bracket(end_col)} AS valid_to
            FROM {q_schema}.{q_table} FOR SYSTEM_TIME ALL
            WHERE {q_pk} = ?
            ORDER BY {_quote_bracket(start_col)} ASC;
            """,
            (pk_val,),
        )
        rows = _rows_to_dicts(cur)
        conn.commit()

        # 將時間轉回本機時區便於閱讀
        for r in rows:
            r["valid_from"] = _utc_to_local(r.get("valid_from"))
            r["valid_to"] = _utc_to_local(r.get("valid_to"))
        return rows

    @staticmethod
    def _apply_row_sync(
        conn,
        database: str,
        info: Dict[str, Any],
        target_map: Dict[str, Dict[str, Any]],
        current_map: Dict[str, Dict[str, Any]],
        only_pk: Optional[str] = None,
    ) -> int:
        """動態產生參數化 T-SQL 同步還原單筆或整表，並自動處理 IDENTITY_INSERT"""
        schema, table = info["schema"], info["table"]
        columns = info["columns"]
        pk_col = info["pk_col"]
        has_identity = info["has_identity"]

        q_db = _quote_bracket(database)
        q_schema = _quote_bracket(schema)
        q_table = _quote_bracket(table)
        q_pk = _quote_bracket(pk_col)

        col_list_sql = ", ".join(_quote_bracket(c) for c in columns)
        placeholders = ", ".join(["?"] * len(columns))
        non_pk_cols = [c for c in columns if c != pk_col]

        affected = 0
        keys_to_process = [only_pk] if only_pk is not None else sorted(set(target_map.keys()) | set(current_map.keys()))

        cur = conn.cursor()
        try:
            cur.execute(f"USE {q_db};")

            # 先檢查是否有需要重新插入的列，且表含有 IDENTITY
            needs_insert = any(
                (target_map.get(k) is not None and current_map.get(k) is None)
                for k in keys_to_process
            )
            if has_identity and needs_insert:
                cur.execute(f"SET IDENTITY_INSERT {q_schema}.{q_table} ON;")

            try:
                for pk_str in keys_to_process:
                    t_row = target_map.get(pk_str)
                    c_row = current_map.get(pk_str)

                    if t_row is not None:
                        if c_row is not None:
                            changed = any(str(c_row.get(c)) != str(t_row.get(c)) for c in columns)
                            if changed and non_pk_cols:
                                set_clause = ", ".join(f"{_quote_bracket(c)} = ?" for c in non_pk_cols)
                                vals = [t_row.get(c) for c in non_pk_cols] + [t_row.get(pk_col)]
                                cur.execute(f"UPDATE {q_schema}.{q_table} SET {set_clause} WHERE {q_pk} = ?;", vals)
                                affected += 1
                        else:
                            vals = [t_row.get(c) for c in columns]
                            cur.execute(f"INSERT INTO {q_schema}.{q_table} ({col_list_sql}) VALUES ({placeholders});", vals)
                            affected += 1
                    else:
                        if c_row is not None:
                            cur.execute(f"DELETE FROM {q_schema}.{q_table} WHERE {q_pk} = ?;", (c_row.get(pk_col),))
                            affected += 1
            finally:
                if has_identity and needs_insert:
                    cur.execute(f"SET IDENTITY_INSERT {q_schema}.{q_table} OFF;")

            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def restore_one(conn, database: str, full_table: str, pk_val: Any, t: datetime) -> bool:
        """將 SQL Server 資料表的單筆資料還原至時間點 t"""
        info = MssqlMonitor.get_table_schema(conn, database, full_table)
        pk_col = info["pk_col"]
        if not pk_col:
            raise ValueError("資料表缺少單一主鍵，無法執行單筆還原。")

        target_rows = MssqlMonitor.query_at(conn, database, full_table, t)
        current_rows = MssqlMonitor.get_current_rows(conn, database, full_table, limit=100000)

        target_map = {str(r[pk_col]): r for r in target_rows}
        current_map = {str(r[pk_col]): r for r in current_rows}

        affected = MssqlMonitor._apply_row_sync(conn, database, info, target_map, current_map, only_pk=str(pk_val))
        return affected > 0

    @staticmethod
    def restore_all(conn, database: str, full_table: str, t: datetime) -> int:
        """將 SQL Server 資料表的整表內容還原至時間點 t"""
        info = MssqlMonitor.get_table_schema(conn, database, full_table)
        pk_col = info["pk_col"]
        if not pk_col:
            raise ValueError("資料表缺少單一主鍵，無法執行整表還原。")

        target_rows = MssqlMonitor.query_at(conn, database, full_table, t)
        current_rows = MssqlMonitor.get_current_rows(conn, database, full_table, limit=100000)

        target_map = {str(r[pk_col]): r for r in target_rows}
        current_map = {str(r[pk_col]): r for r in current_rows}

        return MssqlMonitor._apply_row_sync(conn, database, info, target_map, current_map, only_pk=None)

    @staticmethod
    def storage_size_mb(conn, database: str, full_table: str) -> float:
        """計算資料表（含其系統版本化歷史表）的總佔用空間 (MB)"""
        schema, table = _split_schema_table(full_table)
        q_db = _quote_bracket(database)
        cur = conn.cursor()
        cur.execute(f"USE {q_db};")
        cur.execute(
            """
            SELECT COALESCE(SUM(a.total_pages) * 8.0 / 1024.0, 0.0) AS size_mb
            FROM sys.tables t
            JOIN sys.schemas s ON t.schema_id = s.schema_id
            JOIN sys.indexes i ON t.object_id = i.object_id
            JOIN sys.partitions p ON i.object_id = p.object_id AND i.index_id = p.index_id
            JOIN sys.allocation_units a ON p.partition_id = a.container_id
            WHERE (s.name = ? AND t.name = ?)
               OR t.object_id = (
                   SELECT t2.history_table_id
                   FROM sys.tables t2
                   JOIN sys.schemas s2 ON t2.schema_id = s2.schema_id
                   WHERE s2.name = ? AND t2.name = ?
               );
            """,
            (schema, table, schema, table),
        )
        row = cur.fetchone()
        conn.commit()
        return float(row[0]) if row and row[0] is not None else 0.0

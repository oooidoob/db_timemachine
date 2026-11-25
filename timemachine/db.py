"""
資料庫連線與環境變數管理模組 (db.py)
負責讀取 .env 設定檔、建立 PyMySQL 連線，並在連線失敗時提供友善的繁體中文錯誤診斷。
"""

from __future__ import annotations
import os
import re
from datetime import datetime
from typing import Dict, Any, Optional
try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError as e:
    raise ImportError(
        "【缺少必要套件】未安裝 pymysql 套件！\n"
        "請在終端機執行：pip install -r requirements.txt (或 pip install pymysql)"
    ) from e

try:
    from dotenv import load_dotenv
except ImportError as e:
    raise ImportError(
        "【缺少必要套件】未安裝 python-dotenv 套件！\n"
        "請在終端機執行：pip install -r requirements.txt (或 pip install python-dotenv)"
    ) from e

# 使用相對路徑動態推導專案根目錄，避免寫死絕對路徑
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")

# 載入 .env 環境變數檔（若存在）
load_dotenv(ENV_PATH)


class DatabaseConnectionError(Exception):
    """資料庫連線自訂例外類別，包裝清楚的中文診斷訊息"""
    pass


def get_mysql_config() -> Dict[str, Any]:
    """從環境變數取得 MySQL 8 連線參數（供設計 A、B、C 與基準線使用）"""
    return {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "tm_user"),
        "password": os.getenv("DB_PASSWORD", "tm_password"),
        "database": os.getenv("DB_NAME", "timemachine"),
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": False,
        "connect_timeout": 5,
    }


def get_mariadb_config() -> Dict[str, Any]:
    """從環境變數取得 MariaDB 11 連線參數（供設計 D 原生系統版本化使用）"""
    return {
        "host": os.getenv("MARIADB_HOST", "127.0.0.1"),
        "port": int(os.getenv("MARIADB_PORT", "3307")),
        "user": os.getenv("MARIADB_USER", "tm_user"),
        "password": os.getenv("MARIADB_PASSWORD", "tm_password"),
        "database": os.getenv("MARIADB_NAME", "timemachine"),
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": False,
        "connect_timeout": 5,
    }


def get_connection(db_type: str = "mysql", autocommit: bool = False) -> pymysql.Connection:
    """
    建立並回傳資料庫連線。

    :param db_type: 'mysql' 或 'mariadb'
    :param autocommit: 是否開啟自動認可交易
    :return: pymysql.Connection
    :raises DatabaseConnectionError: 連線失敗時拋出包含繁體中文說明的錯誤
    """
    if db_type == "mariadb":
        config = get_mariadb_config()
        label = f"MariaDB ({config['host']}:{config['port']})"
    else:
        config = get_mysql_config()
        label = f"MySQL ({config['host']}:{config['port']})"

    config["autocommit"] = autocommit

    try:
        conn = pymysql.connect(**config)
        # 設定資料庫 Session 時區與本機 Python 完全一致，避免 Docker 容器預設 UTC 造成時間落差
        try:
            local_tz = datetime.now().astimezone().strftime("%z")
            tz_str = f"{local_tz[:3]}:{local_tz[3:]}" if len(local_tz) == 5 else "+00:00"
            with conn.cursor() as cur:
                cur.execute(f"SET time_zone = '{tz_str}';")
        except Exception:
            pass  # 若權限不足則忽略
        return conn
    except pymysql.err.OperationalError as e:
        err_code, err_msg = e.args[0], e.args[1] if len(e.args) > 1 else str(e)
        if err_code == 2003:
            raise DatabaseConnectionError(
                f"無法連線至 {label}！\n"
                f"【可能原因】Docker 容器尚未啟動，或是通訊埠尚未開放。\n"
                f"【排查建議】請在終端機執行 `docker compose up -d` 啟動資料庫服務。"
            ) from e
        elif err_code == 1045:
            raise DatabaseConnectionError(
                f"連線至 {label} 失敗：使用者名稱或密碼錯誤！\n"
                f"【排查建議】請檢查專案根目錄的 .env 檔案中的 DB_USER 與 DB_PASSWORD 設定。"
            ) from e
        elif err_code == 1049:
            raise DatabaseConnectionError(
                f"連線至 {label} 失敗：資料庫 `{config['database']}` 不存在！\n"
                f"【排查建議】請確認 docker-compose.yml 中的環境變數或執行初始化 SQL。"
            ) from e
        else:
            raise DatabaseConnectionError(
                f"連線至 {label} 時發生運作錯誤 (錯誤碼 {err_code})：{err_msg}"
            ) from e
    except Exception as e:
        raise DatabaseConnectionError(f"連線至 {label} 發生未預期錯誤：{str(e)}") from e


def parse_sql_script(sql_content: str) -> list[str]:
    """
    解析含有 DELIMITER 語法的 SQL 腳本，拆解為獨立執行的 SQL 語句。
    專門處理含有觸發器 (TRIGGER) 或預存程序的語法。
    """
    statements = []
    current_delimiter = ";"
    current_stmt: list[str] = []

    for line in sql_content.splitlines():
        trimmed = line.strip()

        # 忽略單行註解與空白行
        if not trimmed or trimmed.startswith("--"):
            continue

        # 偵測 DELIMITER 變更
        delimiter_match = re.match(r"^DELIMITER\s+(\S+)$", trimmed, re.IGNORECASE)
        if delimiter_match:
            # 切換分隔符號前，若緩衝區有內容則先存入
            if current_stmt:
                stmt_str = "\n".join(current_stmt).strip()
                if stmt_str:
                    statements.append(stmt_str)
                current_stmt = []
            current_delimiter = delimiter_match.group(1)
            continue

        # 檢查該行是否以目前分隔符號結尾
        if trimmed.endswith(current_delimiter):
            # 去除結尾的分隔符號
            content_without_delim = line[:line.rfind(current_delimiter)]
            current_stmt.append(content_without_delim)
            stmt_str = "\n".join(current_stmt).strip()
            if stmt_str:
                statements.append(stmt_str)
            current_stmt = []
        else:
            current_stmt.append(line)

    if current_stmt:
        remaining = "\n".join(current_stmt).strip()
        if remaining:
            statements.append(remaining)

    return statements


def execute_sql_file(conn: pymysql.Connection, file_path: str) -> None:
    """
    讀取並執行指定的 .sql 檔案內容。
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"找不到 SQL 檔案：{file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        sql_content = f.read()

    statements = parse_sql_script(sql_content)
    with conn.cursor() as cursor:
        for stmt in statements:
            if stmt.strip():
                cursor.execute(stmt)
    conn.commit()


def check_db_health() -> Dict[str, Any]:
    """
    檢查 MySQL 與 MariaDB 兩個容器的健康狀態與連線狀況。
    回傳檢查結果字典，方便在測試或介面啟動時快速診斷。
    """
    status: Dict[str, Any] = {
        "mysql": {"ok": False, "message": ""},
        "mariadb": {"ok": False, "message": ""},
    }

    # 檢查 MySQL
    try:
        conn = get_connection("mysql")
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS ver;")
            row = cur.fetchone()
            status["mysql"]["ok"] = True
            status["mysql"]["message"] = f"連線成功 (版本: {row['ver']})"
        conn.close()
    except Exception as e:
        status["mysql"]["ok"] = False
        status["mysql"]["message"] = str(e)

    # 檢查 MariaDB
    try:
        conn = get_connection("mariadb")
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS ver;")
            row = cur.fetchone()
            status["mariadb"]["ok"] = True
            status["mariadb"]["message"] = f"連線成功 (版本: {row['ver']})"
        conn.close()
    except Exception as e:
        status["mariadb"]["ok"] = False
        status["mariadb"]["message"] = str(e)

    return status

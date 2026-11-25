"""
資料庫初始化與診斷腳本 (init_db.py)
一鍵檢查 Docker 資料庫容器連線狀態，並自動執行 schema_mysql.sql 與 schema_mariadb.sql。
"""

import sys
import os
from timemachine.db import (
    get_connection,
    execute_sql_file,
    check_db_health,
    BASE_DIR,
    DatabaseConnectionError,
)


def main():
    print("=" * 60)
    print(" 資料庫時光機：環境連線檢查與結構初始化")
    print("=" * 60)

    # 1. 檢查資料庫健康狀態
    health = check_db_health()
    mysql_ok = health["mysql"]["ok"]
    mariadb_ok = health["mariadb"]["ok"]

    print(f"[1/3] MySQL (埠號 3306) 連線狀態 : {'[OK] 正常' if mysql_ok else '[ERROR] 失敗'}")
    if not mysql_ok:
        print(f"      錯誤說明: {health['mysql']['message']}")

    print(f"[2/3] MariaDB (埠號 3307) 連線狀態: {'[OK] 正常' if mariadb_ok else '[ERROR] 失敗'}")
    if not mariadb_ok:
        print(f"      錯誤說明: {health['mariadb']['message']}")

    if not mysql_ok and not mariadb_ok:
        print("\n【診斷與解決步驟】")
        print("尚未偵測到任何資料庫服務。請確認：")
        print("  1. 已安裝並啟動 Docker Desktop")
        print("  2. 在專案根目錄執行: docker compose up -d (或 docker-compose up -d)")
        print("  3. 待容器完全啟動後，重新執行 python init_db.py")
        sys.exit(1)

    # 2. 初始化 MySQL 結構與觸發器
    if mysql_ok:
        print("\n[3/3] 正在載入與初始化 MySQL 結構 (schema_mysql.sql)...")
        mysql_schema_path = os.path.join(BASE_DIR, "schema_mysql.sql")
        try:
            conn = get_connection("mysql")
            execute_sql_file(conn, mysql_schema_path)
            conn.close()
            print("      [OK] MySQL 結構與觸發器初始化成功！")
        except Exception as e:
            print(f"      [ERROR] MySQL 初始化失敗: {e}")

    # 3. 初始化 MariaDB 結構
    if mariadb_ok:
        print("      正在載入與初始化 MariaDB 結構 (schema_mariadb.sql)...")
        mariadb_schema_path = os.path.join(BASE_DIR, "schema_mariadb.sql")
        try:
            conn = get_connection("mariadb")
            execute_sql_file(conn, mariadb_schema_path)
            conn.close()
            print("      [OK] MariaDB 結構 (WITH SYSTEM VERSIONING) 初始化成功！")
        except Exception as e:
            print(f"      [ERROR] MariaDB 初始化失敗: {e}")

    print("\n" + "=" * 60)
    print(" 資料庫時光機環境準備完成！可以開始執行測試或基準測試。")
    print("=" * 60)


if __name__ == "__main__":
    main()

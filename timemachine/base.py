"""
抽象基底類別模組 (base.py)
定義四種資料變更歷史設計共用的抽象基底類別 BaseTimeMachine。
確保設計 A、B、C、D 具備完全一致的方法簽名，方便在基準測試與 Streamlit 中公平比較與抽換。
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Dict, Any, Optional
import pymysql


class BaseTimeMachine(ABC):
    """
    時光機抽象基底類別 (Abstract Base Class)
    定義所有版本控制設計必須實作的核心方法。
    """

    def __init__(self, db_type: str = "mysql"):
        """
        初始化時光機基底類別。
        :param db_type: 資料庫類型 ('mysql' 或 'mariadb')
        """
        self.db_type = db_type
        self.conn: Optional[pymysql.Connection] = None

    @abstractmethod
    def get_connection(self) -> pymysql.Connection:
        """取得或建立資料庫連線"""
        pass

    @abstractmethod
    def reset_data(self) -> None:
        """
        清空該設計所屬的所有資料表與歷史紀錄。
        用於每次實驗或測試前的環境初始化。
        """
        pass

    @abstractmethod
    def apply_insert(
        self,
        account_id: int,
        name: str,
        balance: int,
        timestamp: Optional[datetime] = None
    ) -> None:
        """
        新增帳戶（初始資料建立）。
        :param account_id: 帳戶識別碼 (PK)
        :param name: 帳戶名稱
        :param balance: 帳戶餘額
        :param timestamp: 指定建立時間點（若無則取系統目前微秒時間）
        """
        pass

    @abstractmethod
    def apply_update(
        self,
        account_id: int,
        new_balance: int,
        new_name: Optional[str] = None,
        timestamp: Optional[datetime] = None
    ) -> None:
        """
        更新指定帳戶的餘額（及可選名稱）。
        :param account_id: 帳戶識別碼
        :param new_balance: 新餘額
        :param new_name: 新名稱（若為 None 則維持原名稱）
        :param timestamp: 指定更新時間點（若無則取系統目前微秒時間）
        """
        pass

    @abstractmethod
    def apply_delete(
        self,
        account_id: int,
        timestamp: Optional[datetime] = None
    ) -> None:
        """
        刪除指定帳戶。
        :param account_id: 帳戶識別碼
        :param timestamp: 指定刪除時間點（若無則取系統目前微秒時間）
        """
        pass

    @abstractmethod
    def query_at(self, t: datetime) -> List[Dict[str, Any]]:
        """
        時間點查詢：回傳時間點 t 當下整張主表的所有帳戶狀態。
        :param t: 目標時間點 (datetime 物件)
        :return: 帳戶字典列表 [{'id': int, 'name': str, 'balance': int, 'updated_at': datetime}]
        """
        pass

    @abstractmethod
    def query_one_at(self, account_id: int, t: datetime) -> Optional[Dict[str, Any]]:
        """
        單筆時間點查詢：回傳時間點 t 當下該帳戶的狀態。
        :param account_id: 帳戶識別碼
        :param t: 目標時間點
        :return: 帳戶字典；若當時帳戶尚未建立或已被刪除，回傳 None
        """
        pass

    @abstractmethod
    def restore_one(self, account_id: int, t: datetime) -> bool:
        """
        單筆還原：將指定帳戶還原到時間點 t 的狀態。
        - 若時間點 t 該帳戶存在：將目前主表更新為當時的 (name, balance)；若主表中已被刪除則重新插入。
        - 若時間點 t 該帳戶不存在（未建立或已刪除）：若主表目前存在該筆則執行刪除。
        :param account_id: 帳戶識別碼
        :param t: 目標時間點
        :return: 是否有實質變更狀態
        """
        pass

    @abstractmethod
    def restore_all(self, t: datetime) -> int:
        """
        整表還原：將整張主表還原到時間點 t 的完整狀態。
        :param t: 目標時間點
        :return: 影響/變更的資料列筆數
        """
        pass

    @abstractmethod
    def storage_size_mb(self) -> float:
        """
        空間成本查詢：回傳該設計相關資料表（主表、歷史表/快照表/事件表）的總佔用空間 (MB)。
        查詢 information_schema.TABLES 中的 data_length + index_length。
        :return: 佔用空間 (MB，浮點數)
        """
        pass

    @abstractmethod
    def get_history(self, account_id: int) -> List[Dict[str, Any]]:
        """
        帳戶歷史時間軸：取得單一帳戶從古至今的所有變更紀錄。
        供 Streamlit 介面繪製時間軸使用。
        :param account_id: 帳戶識別碼
        :return: 依時間排序的歷史紀錄列表
        """
        pass

    def close(self) -> None:
        """關閉資料庫連線"""
        if self.conn and self.conn.open:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

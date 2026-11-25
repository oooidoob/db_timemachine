"""
資料庫時光機 (Database Time Machine) 套件
四種資料變更歷史設計的時光機實作 + 通用即時監控模組：
- 設計 A：SnapshotTimeMachine（定期快照）
- 設計 B：TriggerHistoryTimeMachine（觸發器 ＋ 歷史表）
- 設計 C：EventSourcingTimeMachine（事件溯源 ＋ 檢查點）
- 設計 D：NativeTemporalTimeMachine（MariaDB 原生系統版本化）
- 通用監控：GenericMonitor（任意資料庫與任意自訂資料表追蹤與還原）
"""

from .base import BaseTimeMachine
from .snapshot import SnapshotTimeMachine
from .trigger_history import TriggerHistoryTimeMachine
from .event_sourcing import EventSourcingTimeMachine
from .native_temporal import NativeTemporalTimeMachine
from .generic_monitor import GenericMonitor, connect_custom
from .mssql_monitor import MssqlMonitor, connect_mssql

__all__ = [
    "BaseTimeMachine",
    "SnapshotTimeMachine",
    "TriggerHistoryTimeMachine",
    "EventSourcingTimeMachine",
    "NativeTemporalTimeMachine",
    "GenericMonitor",
    "connect_custom",
    "MssqlMonitor",
    "connect_mssql",
]

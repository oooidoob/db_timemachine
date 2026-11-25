"""
資料庫時光機：Streamlit 互動式視覺化介面 (app.py)
支援雙模式運作：
1. 🔬 專題實驗模式 (A/B/C/D 比較)：針對示範銀行帳戶模型，比較四種架構設計並一鍵模擬災難與還原。
2. 🌐 通用即時監控模式 (任意資料庫/資料表)：連接任何 MySQL / MariaDB 伺服器，動態探索使用者自建的
   任意資料庫與資料表，一鍵掛載即時監控與時光機追蹤、即時監看變動、比對差異與還原。
"""

from __future__ import annotations
import os
import sys
import time
from datetime import datetime, timedelta
import pandas as pd
import streamlit as st

# 將專案根目錄加入模組搜尋路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from timemachine.db import check_db_health, get_mysql_config, get_mariadb_config
from timemachine.snapshot import SnapshotTimeMachine
from timemachine.trigger_history import TriggerHistoryTimeMachine
from timemachine.event_sourcing import EventSourcingTimeMachine
from timemachine.native_temporal import NativeTemporalTimeMachine
from timemachine.generic_monitor import GenericMonitor, connect_custom
from timemachine.mssql_monitor import MssqlMonitor, connect_mssql


# ==============================================================================
# 0. 頁面組態設定
# ==============================================================================
st.set_page_config(
    page_title="資料庫時光機 (DB Time Machine)",
    page_icon="⏳",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ==============================================================================
# 共用輔助函式：差異比對與上色
# ==============================================================================
def highlight_diff(row):
    """依據差異狀態欄位套用醒目背景色"""
    st_val = str(row.get("差異狀態", ""))
    if "🔴" in st_val:
        return ["background-color: #ffcccc; color: #900; font-weight: bold;"] * len(row)
    elif "🟡" in st_val:
        return ["background-color: #fff2cc; color: #996600; font-weight: bold;"] * len(row)
    elif "🟢" in st_val:
        return ["background-color: #d9ead3; color: #274e13;"] * len(row)
    return [""] * len(row)


def render_timestamp_picker(state_key: str = "target_dt", show_scenario_btn: bool = False) -> datetime:
    """繪製統一的時間點選擇器 (日期 + 微秒時間 + 快捷按鈕)"""
    if state_key not in st.session_state:
        st.session_state[state_key] = datetime.now()
    if f"{state_key}_date" not in st.session_state:
        st.session_state[f"{state_key}_date"] = st.session_state[state_key].date()
    if f"{state_key}_time" not in st.session_state:
        st.session_state[f"{state_key}_time"] = st.session_state[state_key].strftime("%H:%M:%S.%f")

    def _apply_dt(new_dt: datetime):
        st.session_state[state_key] = new_dt
        st.session_state[f"{state_key}_date"] = new_dt.date()
        st.session_state[f"{state_key}_time"] = new_dt.strftime("%H:%M:%S.%f")

    def _on_now():
        _apply_dt(datetime.now())

    def _on_scen():
        if st.session_state.get("scenario_time"):
            _apply_dt(st.session_state["scenario_time"])

    def _on_back_30s():
        curr = st.session_state.get(state_key, datetime.now())
        _apply_dt(curr - timedelta(seconds=30))

    c1, c2, c3, c4 = st.columns([2, 2, 1.5, 1.5])
    with c1:
        target_date = st.date_input("日期", key=f"{state_key}_date")
    with c2:
        time_str = st.text_input("時間 (HH:MM:SS.ffffff)", key=f"{state_key}_time")
        try:
            parsed_time = datetime.strptime(time_str.strip(), "%H:%M:%S.%f").time()
        except Exception:
            try:
                parsed_time = datetime.strptime(time_str.strip(), "%H:%M:%S").time()
            except Exception:
                parsed_time = st.session_state[state_key].time()

    try:
        chosen_dt = datetime.combine(target_date, parsed_time)
        st.session_state[state_key] = chosen_dt
    except Exception:
        chosen_dt = datetime.now()

    with c3:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        st.button("⏱️ 設為現在", use_container_width=True, key=f"{state_key}_now", on_click=_on_now)

    with c4:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        if show_scenario_btn and st.session_state.get("scenario_time"):
            st.button("🎯 回到災難前", use_container_width=True, key=f"{state_key}_scen", on_click=_on_scen)
        else:
            st.button("⏪ 30 秒前", use_container_width=True, key=f"{state_key}_30s", on_click=_on_back_30s)

    return chosen_dt


# ==============================================================================
# 模式 1：專題實驗模式（四種設計比較 A/B/C/D）
# ==============================================================================
def get_timemachine(design_key: str):
    if design_key == "設計 A (定期快照)":
        return SnapshotTimeMachine(auto_snapshot=False)
    elif design_key == "設計 B (觸發器+歷史表)":
        return TriggerHistoryTimeMachine()
    elif design_key == "設計 C (事件溯源)":
        return EventSourcingTimeMachine(checkpoint_interval=10)
    elif design_key == "設計 D (MariaDB原生時態)":
        return NativeTemporalTimeMachine()
    raise ValueError(f"未知的設計類型: {design_key}")


def render_experiment_mode():
    selected_design = st.sidebar.selectbox(
        "選擇歷史架構設計：",
        [
            "設計 A (定期快照)",
            "設計 B (觸發器+歷史表)",
            "設計 C (事件溯源)",
            "設計 D (MariaDB原生時態)",
        ],
        index=1,
    )
    tm = get_timemachine(selected_design)

    health = check_db_health()
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🔌 資料庫健康狀態")
    col_s1, col_s2 = st.sidebar.columns(2)
    with col_s1:
        st.caption("MySQL 8 (3306)")
        if health["mysql"]["ok"]:
            st.success("✅ 運作中")
        else:
            st.error("❌ 未連線")
    with col_s2:
        st.caption("MariaDB 11 (3307)")
        if health["mariadb"]["ok"]:
            st.success("✅ 運作中")
        else:
            st.warning("⚠️ 未連線")

    try:
        current_size = tm.storage_size_mb()
        st.sidebar.metric(label="💾 相關資料表總大小", value=f"{current_size:.3f} MB")
    except Exception:
        st.sidebar.metric(label="💾 相關資料表總大小", value="無法讀取")

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🛠️ 示範與測試情境")

    if st.sidebar.button("💥 一鍵製造情境 (誤刪與篡改)", use_container_width=True, type="primary"):
        with st.spinner("正在注入災難情境..."):
            for aid, name, bal in [(88, "張小明 (Acc88)", 50000), (89, "李美麗 (Acc89)", 35000)]:
                try:
                    tm.apply_insert(aid, name, bal)
                except Exception:
                    tm.apply_update(aid, new_balance=bal, new_name=name)

            if isinstance(tm, SnapshotTimeMachine):
                tm.take_snapshot()

            time.sleep(0.05)
            safe_time = datetime.now()
            time.sleep(0.05)

            tm.apply_delete(88)
            tm.apply_update(89, new_balance=-999999, new_name="HACKED_李美麗")

            if isinstance(tm, SnapshotTimeMachine):
                tm.take_snapshot()

            st.session_state["target_dt"] = safe_time
            st.session_state["target_dt_date"] = safe_time.date()
            st.session_state["target_dt_time"] = safe_time.strftime("%H:%M:%S.%f")
            st.session_state["scenario_active"] = True
            st.session_state["scenario_time"] = safe_time
            st.rerun()

    if st.sidebar.button("🌱 建立示範種子資料 (5筆帳戶)", use_container_width=True):
        with st.spinner("正在建立示範資料..."):
            sample_accounts = [
                (1, "王大同", 12000),
                (2, "陳美玲", 8500),
                (3, "林志豪", 43000),
                (4, "黃雅婷", 19500),
                (5, "張志強", 31000),
            ]
            for aid, name, bal in sample_accounts:
                try:
                    tm.apply_insert(aid, name, bal)
                except Exception:
                    tm.apply_update(aid, new_balance=bal, new_name=name)
            if isinstance(tm, SnapshotTimeMachine):
                tm.take_snapshot()
            st.sidebar.success("✅ 示範資料已建立！")
            st.rerun()

    if st.sidebar.button("🗑️ 清空此設計所有資料", use_container_width=True):
        tm.reset_data()
        st.sidebar.info("已清空資料表與歷史紀錄。")
        st.rerun()

    # 主畫面
    st.title("🕰️ 資料庫時光機：四種架構實驗對照")
    st.markdown(f"當前模式：**`{selected_design}`** *(微秒精度 DATETIME(6) 時間旅行與災難還原)*")

    if st.session_state.get("scenario_active"):
        st.warning(
            "🚨 **偵測到模擬災難事件！**  \n"
            "- **帳戶 #88** (張小明) 遭到不當刪除！  \n"
            "- **帳戶 #89** (李美麗) 餘額遭到惡意篡改為負數！  \n"
            "💡 **建議動作**：目標時間點已鎖定至事故發生前，請使用下方 **「還原此筆」** 或 **「還原整表」** 進行救援！"
        )

    st.markdown("### 1. 選擇要回溯的歷史時間點 (AS OF Timestamp)")
    chosen_datetime = render_timestamp_picker("target_dt", show_scenario_btn=True)

    st.markdown("---")
    st.markdown(f"### 2. 雙表比對：時間點 `{chosen_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')}` vs 目前最新狀態")

    try:
        historical_rows = tm.query_at(chosen_datetime)
        current_rows = tm.query_at(datetime.now())
    except Exception as e:
        st.error(f"查詢失敗：{e}")
        historical_rows, current_rows = [], []

    hist_map = {r["id"]: r for r in historical_rows}
    curr_map = {r["id"]: r for r in current_rows}
    all_ids = sorted(list(set(hist_map.keys()) | set(curr_map.keys())))

    diff_data = []
    for aid in all_ids:
        h, c = hist_map.get(aid), curr_map.get(aid)
        if h is not None and c is None:
            status = "🔴 目前已被刪除"
        elif h is None and c is not None:
            status = "🟢 該時間點後才新增"
        elif h["name"] != c["name"] or h["balance"] != c["balance"]:
            status = "🟡 餘額或名稱已變更"
        else:
            status = "⚪ 內容完全相同"

        diff_data.append({
            "帳戶 ID": aid,
            "歷史名稱": h["name"] if h else "— (未建立或已刪除)",
            "歷史餘額": f"${h['balance']:,}" if h else "—",
            "當前名稱": c["name"] if c else "— (已刪除)",
            "當前餘額": f"${c['balance']:,}" if c else "—",
            "差異狀態": status,
        })

    col_tbl1, col_tbl2 = st.columns(2)
    with col_tbl1:
        st.markdown(f"#### 📜 時間點之歷史狀態 (共 {len(historical_rows)} 筆)")
        if historical_rows:
            st.dataframe(pd.DataFrame(historical_rows)[["id", "name", "balance", "updated_at"]], use_container_width=True, height=250)
        else:
            st.info("該時間點無任何帳戶資料。")

    with col_tbl2:
        st.markdown(f"#### 🏢 目前最新主表狀態 (共 {len(current_rows)} 筆)")
        if current_rows:
            st.dataframe(pd.DataFrame(current_rows)[["id", "name", "balance", "updated_at"]], use_container_width=True, height=250)
        else:
            st.info("目前主表無任何帳戶。")

    st.markdown("#### 🔍 差異比對分析總表 (Diff Analysis)")
    if diff_data:
        st.dataframe(pd.DataFrame(diff_data).style.apply(highlight_diff, axis=1), use_container_width=True, height=260)
    else:
        st.info("無任何資料可供比對。")

    st.markdown("---")
    st.markdown("### 3. 時光還原操作 (Restore to Timestamp)")
    r_col1, r_col2 = st.columns(2)
    with r_col1:
        st.markdown("#### 🎯 單筆還原 (Restore One)")
        restore_acc_id = st.number_input("輸入欲還原的帳戶 ID：", min_value=1, value=88, step=1)
        if st.button("🔄 執行單筆還原", use_container_width=True):
            try:
                if tm.restore_one(restore_acc_id, chosen_datetime):
                    st.success(f"✅ 帳戶 #{restore_acc_id} 已成功還原！")
                    st.rerun()
                else:
                    st.info(f"帳戶 #{restore_acc_id} 與目標狀態已完全相符。")
            except Exception as e:
                st.error(f"單筆還原失敗：{e}")

    with r_col2:
        st.markdown("#### 🚀 整表還原 (Restore All)")
        st.markdown("<div style='height: 44px;'></div>", unsafe_allow_html=True)
        if st.button("⚠️ 執行全表一鍵還原 (Restore All)", use_container_width=True, type="primary"):
            try:
                affected = tm.restore_all(chosen_datetime)
                st.success(f"🎉 整表還原成功！共變更 {affected} 筆資料列。")
                st.session_state["scenario_active"] = False
                st.rerun()
            except Exception as e:
                st.error(f"全表還原失敗：{e}")

    st.markdown("---")
    st.markdown("### 4. 單一帳戶歷史軌跡時間軸 (Account Audit Timeline)")
    timeline_id = st.number_input("查看歷史變更歷程的帳戶 ID：", min_value=1, value=88, step=1, key="timeline_acc_id")
    try:
        history_records = tm.get_history(timeline_id)
        if history_records:
            df_hist = pd.DataFrame(history_records)
            display_cols = [c for c in ["valid_from", "valid_to", "name", "balance", "event_type", "record_type"] if c in df_hist.columns]
            st.dataframe(df_hist[display_cols], use_container_width=True)
        else:
            st.info(f"帳戶 #{timeline_id} 尚無任何變更紀錄。")
    except Exception as e:
        st.error(f"讀取時間軸失敗：{e}")


# ==============================================================================
# 模式 2：通用即時監控模式（任意資料庫 ＆ 任意資料表）
# ==============================================================================
def render_universal_monitor_mode() -> bool:
    """渲染通用即時監控模式（支援 MySQL 8、MariaDB 11、SQL Server），回傳是否開啟自動刷新"""
    st.sidebar.markdown("### 🔌 資料庫連線設定")
    engine_choice = st.sidebar.radio(
        "資料庫引擎：",
        ["MySQL 8", "MariaDB 11", "SQL Server (MSSQL)"],
        horizontal=False,
    )

    if engine_choice == "SQL Server (MSSQL)":
        db_type = "mssql"
        with st.sidebar.expander("🔧 SQL Server 連線參數", expanded=True):
            server = st.text_input("伺服器名稱 (Server)", value="localhost", help="預設為本機 localhost；若為 Express 版請填 localhost\\SQLEXPRESS")
            trusted = st.checkbox("使用 Windows 驗證 (免密碼，同 SSMS)", value=True)
            user, password = "", ""
            if not trusted:
                user = st.text_input("登入帳號 (User)", value="sa")
                password = st.text_input("密碼 (Password)", value="", type="password")
        conn_label = f"{server} ({'Windows 驗證' if trusted else user})"
    else:
        db_type = "mysql" if engine_choice == "MySQL 8" else "mariadb"
        default_cfg = get_mysql_config() if db_type == "mysql" else get_mariadb_config()
        with st.sidebar.expander("🔧 自訂連線參數 (預設讀取 .env)", expanded=False):
            host = st.text_input("主機 (Host)", value=default_cfg["host"])
            port = st.number_input("埠號 (Port)", value=int(default_cfg["port"]), min_value=1, max_value=65535)
            user = st.text_input("帳號 (User)", value=default_cfg["user"])
            password = st.text_input("密碼 (Password)", value=default_cfg["password"], type="password")
        conn_label = f"{host}:{port}"

    # 連線至目標伺服器
    try:
        if db_type == "mssql":
            conn = connect_mssql(server=server, trusted_connection=trusted, user=user, password=password)
        else:
            conn = connect_custom(host, port, user, password, db_type=db_type)
        st.sidebar.success(f"✅ 已連線至 {engine_choice} ({conn_label})")
    except Exception as e:
        st.title("🌐 通用資料庫即時監控與時光機")
        st.error(f"❌ 連線失敗：{e}")
        if db_type == "mssql":
            st.info("若尚未安裝 SQL Server 驅動套件，請先在終端機執行：`pip install pyodbc`")
        else:
            st.info("請確認資料庫已啟動，或在左側欄「🔧 自訂連線參數」填寫正確的主機、埠號與帳號密碼。")
        return False

    # 統一操作介面轉接（消除 MySQL/MariaDB 與 MSSQL 的重複代碼）
    if db_type == "mssql":
        list_dbs = lambda c: MssqlMonitor.list_databases(c)
        list_tbls = lambda c, d: MssqlMonitor.list_tables(c, d)
        get_schema = lambda c, d, t: MssqlMonitor.get_table_schema(c, d, t)
        is_tracked_fn = lambda c, d, t: MssqlMonitor.is_tracking_enabled(c, d, t)
        enable_fn = lambda c, d, t: (MssqlMonitor.enable_tracking(c, d, t) or 0)
        get_curr_fn = lambda c, d, t: MssqlMonitor.get_current_rows(c, d, t, limit=500)
        size_fn = lambda c, d, t: MssqlMonitor.storage_size_mb(c, d, t)
        query_at_fn = lambda c, d, t, dt: MssqlMonitor.query_at(c, d, t, dt)
        restore_one_fn = lambda c, d, t, pk, dt: MssqlMonitor.restore_one(c, d, t, pk, dt)
        restore_all_fn = lambda c, d, t, dt: MssqlMonitor.restore_all(c, d, t, dt)
        history_fn = lambda c, d, t, pk: MssqlMonitor.get_row_history(c, d, t, pk)
    else:
        list_dbs = lambda c: GenericMonitor.list_databases(c)
        list_tbls = lambda c, d: GenericMonitor.list_tables(c, d)
        get_schema = lambda c, d, t: GenericMonitor.get_table_schema(c, d, t)
        is_tracked_fn = lambda c, d, t: GenericMonitor.is_tracking_enabled(c, d, t, db_type)
        enable_fn = lambda c, d, t: GenericMonitor.enable_tracking(c, d, t, db_type)
        get_curr_fn = lambda c, d, t: GenericMonitor.get_current_rows(c, d, t, limit=500)
        size_fn = lambda c, d, t: GenericMonitor.storage_size_mb(c, d, t, db_type)
        query_at_fn = lambda c, d, t, dt: GenericMonitor.query_at(c, d, t, dt, db_type)
        restore_one_fn = lambda c, d, t, pk, dt: GenericMonitor.restore_one(c, d, t, pk, dt, db_type)
        restore_all_fn = lambda c, d, t, dt: GenericMonitor.restore_all(c, d, t, dt, db_type)
        history_fn = lambda c, d, t, pk: GenericMonitor.get_row_history(c, d, t, pk, db_type)

    try:
        databases = list_dbs(conn)
        if not databases:
            st.title("🌐 通用資料庫即時監控與時光機")
            st.warning("目前伺服器上找不到任何使用者資料庫，請先建立資料庫（例如 `CREATE DATABASE MyTestDB;`）。")
            return False

        selected_db = st.sidebar.selectbox("📂 選擇監控資料庫 (Database)：", databases)
        tables = list_tbls(conn, selected_db)

        st.sidebar.markdown("---")
        col_r1, col_r2 = st.sidebar.columns(2)
        with col_r1:
            if st.button("🔄 立即刷新", use_container_width=True):
                st.rerun()
        with col_r2:
            auto_refresh = st.checkbox("⏱️ 自動監控 (3秒)", value=False)

        st.title("🌐 通用資料庫即時監控與時光機")
        st.caption(
            f"連線目標：`{engine_choice}` (`{conn_label}`) ｜ "
            f"最後刷新時間：`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
        )

        if not tables:
            st.info(f"資料庫 `{selected_db}` 目前沒有任何資料表。您可以隨時建立新表，點擊左側「🔄 立即刷新」即可看見。")
            return auto_refresh

        selected_table = st.sidebar.selectbox("📋 選擇監控資料表 (Table)：", tables)
        schema = get_schema(conn, selected_db, selected_table)
        columns = schema["columns"]
        pk_col = schema["pk_col"]
        is_tracked = is_tracked_fn(conn, selected_db, selected_table)

        # 頂部監控指標列
        m1, m2, m3, m4 = st.columns(4)
        current_rows = get_curr_fn(conn, selected_db, selected_table)
        size_mb = size_fn(conn, selected_db, selected_table)
        m1.metric("監控目標", f"{selected_db}.{selected_table}")
        m2.metric("目前資料列數 (上限500)", f"{len(current_rows)} 筆")
        m3.metric("資料表總大小", f"{size_mb:.3f} MB")
        m4.metric("時光機狀態", "✅ 已啟用錄影" if is_tracked else "⚪ 未啟用")

        # ------------------------------------------------------------------
        # 時光機掛載控制區塊
        # ------------------------------------------------------------------
        if not is_tracked:
            st.warning(
                f"⚠️ 資料表 `{selected_db}.{selected_table}` 目前為**普通資料表**，尚未掛載時光機追蹤。  \n"
                f"您可以即時監看它的目前內容；若想記錄每一次異動與支持回溯還原，請點擊下方按鈕一鍵啟用："
            )
            if db_type in ("mysql", "mssql") and not pk_col:
                st.error("❌ 此表沒有「單一欄位主鍵 (Primary Key)」，請先設定主鍵（例如 `id INT PRIMARY KEY`）後再啟用時光機。")
            else:
                if db_type == "mssql":
                    btn_label = f"⚡ 一鍵啟用 SQL Server 原生時光機 (SYSTEM_VERSIONING = ON)"
                elif db_type == "mariadb":
                    btn_label = "⚡ 一鍵啟用 MariaDB 原生時光機 (ADD SYSTEM VERSIONING)"
                else:
                    btn_label = f"⚡ 一鍵為 `{selected_table}` 掛載即時監控觸發器 (建立 {selected_table}_tm_history)"

                if st.button(btn_label, type="primary"):
                    try:
                        init_cnt = enable_fn(conn, selected_db, selected_table)
                        st.success(f"🎉 已成功啟用時光機！（{f'已保留既有 {init_cnt} 筆初始快照' if db_type == 'mysql' else '原生版本控制已開啟'}）")
                        st.rerun()
                    except Exception as e:
                        st.error(f"啟用失敗：{e}")

        # ------------------------------------------------------------------
        # 即時資料監控視圖
        # ------------------------------------------------------------------
        st.markdown("### 📡 即時資料表內容 (Live View)")
        if current_rows:
            st.dataframe(pd.DataFrame(current_rows), use_container_width=True, height=260)
        else:
            st.info(f"資料表 `{selected_db}.{selected_table}` 目前為空。")

        # ------------------------------------------------------------------
        # 若已啟用時光機：提供時間點對比、Diff 高亮、還原與單筆履歷
        # ------------------------------------------------------------------
        if is_tracked:
            st.markdown("---")
            st.markdown("### 🕰️ 時間點旅行與異動差異比對")
            chosen_dt = render_timestamp_picker("univ_target_dt", show_scenario_btn=False)

            hist_rows = query_at_fn(conn, selected_db, selected_table, chosen_dt)

            c_left, c_right = st.columns(2)
            with c_left:
                st.markdown(f"#### 📜 `{chosen_dt.strftime('%H:%M:%S.%f')}` 歷史狀態 ({len(hist_rows)} 筆)")
                if hist_rows:
                    st.dataframe(pd.DataFrame(hist_rows), use_container_width=True, height=240)
                else:
                    st.info("該時間點尚無資料（或當時尚未啟用時光機）。")

            with c_right:
                st.markdown(f"#### 🏢 目前最新狀態 ({len(current_rows)} 筆)")
                if current_rows:
                    st.dataframe(pd.DataFrame(current_rows), use_container_width=True, height=240)
                else:
                    st.info("目前資料表為空。")

            # 差異比對 (Diff)
            if pk_col:
                h_map = {str(r[pk_col]): r for r in hist_rows}
                c_map = {str(r[pk_col]): r for r in current_rows}
                all_pks = sorted(set(h_map.keys()) | set(c_map.keys()))

                diff_list = []
                for pk in all_pks:
                    hr, cr = h_map.get(pk), c_map.get(pk)
                    if hr is not None and cr is None:
                        st_label = "🔴 目前已被刪除"
                    elif hr is None and cr is not None:
                        st_label = "🟢 該時間點後才新增"
                    elif any(str(hr.get(col)) != str(cr.get(col)) for col in columns):
                        st_label = "🟡 欄位內容已變更"
                    else:
                        st_label = "⚪ 內容完全相同"

                    row_summary = {pk_col: pk, "差異狀態": st_label}
                    for col in columns:
                        if col != pk_col:
                            h_val = hr.get(col) if hr else "—"
                            c_val = cr.get(col) if cr else "—"
                            row_summary[f"{col} (歷史 → 目前)"] = f"{h_val}" if str(h_val) == str(c_val) else f"{h_val} → {c_val}"
                    diff_list.append(row_summary)

                st.markdown("#### 🔍 逐列差異比對 (Diff)")
                if diff_list:
                    st.dataframe(pd.DataFrame(diff_list).style.apply(highlight_diff, axis=1), use_container_width=True, height=250)

                # 還原控制區
                st.markdown("---")
                st.markdown("### 🚑 任意資料表救援還原")
                rc1, rc2 = st.columns(2)
                with rc1:
                    target_pk = st.text_input(f"輸入要還原的 `{pk_col}` 主鍵值：", value=all_pks[0] if all_pks else "")
                    if st.button("🔄 單筆還原至目標時間點", use_container_width=True):
                        try:
                            if restore_one_fn(conn, selected_db, selected_table, target_pk, chosen_dt):
                                st.success(f"✅ 主鍵 `{pk_col}={target_pk}` 已還原！")
                                st.rerun()
                            else:
                                st.info("該筆資料與目標時間點完全相符，無需還原。")
                        except Exception as e:
                            st.error(f"單筆還原失敗：{e}")

                with rc2:
                    st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                    if st.button("⚠️ 全表一鍵還原至目標時間點", use_container_width=True, type="primary"):
                        try:
                            cnt = restore_all_fn(conn, selected_db, selected_table, chosen_dt)
                            st.success(f"🎉 全表還原完成！共校正 {cnt} 筆資料。")
                            st.rerun()
                        except Exception as e:
                            st.error(f"全表還原失敗：{e}")

                # 單筆異動時間軸
                st.markdown("---")
                st.markdown(f"### 📜 單筆資料完整異動時間軸 (依主鍵 `{pk_col}`)")
                hist_pk = st.text_input(f"查詢異動履歷的 `{pk_col}`：", value=all_pks[0] if all_pks else "", key="univ_hist_pk")
                if hist_pk:
                    row_hist = history_fn(conn, selected_db, selected_table, hist_pk)
                    if row_hist:
                        st.dataframe(pd.DataFrame(row_hist), use_container_width=True)
                    else:
                        st.info(f"找不到 `{pk_col}={hist_pk}` 的歷史異動紀錄。")

        return auto_refresh
    finally:
        conn.close()


# ==============================================================================
# 主程式進入點：側邊欄模式切換
# ==============================================================================
st.sidebar.title("⏳ 資料庫時光機")
app_mode = st.sidebar.radio(
    "選擇系統模式：",
    [
        "🔬 專題實驗模式 (A/B/C/D 比較)",
        "🌐 通用即時監控模式 (任意資料庫/資料表)",
    ],
    index=0,
)
st.sidebar.markdown("---")

auto_refresh_needed = False
if app_mode == "🔬 專題實驗模式 (A/B/C/D 比較)":
    render_experiment_mode()
else:
    auto_refresh_needed = render_universal_monitor_mode()

st.markdown("---")
st.caption("大學資料庫課程專題：資料庫時光機 (Database Time Machine) | 支援四種設計架構評測與任意資料庫即時監控")

if auto_refresh_needed:
    time.sleep(3)
    st.rerun()

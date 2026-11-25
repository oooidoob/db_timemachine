# 🕰️ 資料庫時光機 (Database Time Machine)
### 比較四種資料變更歷史設計的空間與查詢成本

> 本專題為大學資料庫系統（Database Systems）課程之個人期末專案。
> 探討關聯式資料庫如何記錄每筆資料的每次變更、支援精確至微秒等級的時間點旅行查詢（Point-in-Time Query），以及災難發生時的無損回溯還原。

---

## 🚀 懶人快速體驗（免裝 Docker，只要 2 步）

如果你已經有自己的資料庫（例如 **SQL Server**、**MySQL** 或 **MariaDB**），只想直接打開網頁體驗「即時監控任意資料表 ＋ 一鍵還原」，**不需要執行下方繁瑣的 Docker 與測試步驟**，下載專案後只要執行這兩行即可：

```powershell
# 1. 安裝必要套件
pip install -r requirements.txt

# 2. 啟動視覺化網頁介面
streamlit run app.py
```

### 📱 互動網頁介面 (`app.py`) 兩大核心模式

#### 1. 🔬 專題實驗模式（A / B / C / D 四種設計比較）
- 針對「銀行帳戶」示範模型，自由切換四種歷史架構（定期快照、觸發器歷史表、事件溯源、MariaDB 原生版本化），即時對比儲存空間（MB）。
- 提供 **「一鍵製造情境」** 按鈕，自動模擬帳戶遭誤刪與惡意篡改，並以紅、黃、綠三色高亮標示差異，執行單筆／全表還原與完整時間軸稽核。

#### 2. 🌐 通用即時監控模式（任意資料庫 ＆ 任意資料表）
> 本專案最重要的實務延伸實作：無須修改原有系統程式碼，即可對任意自建資料庫與任意欄位結構的資料表進行即時監控與時光機救援。

- **支援三大主流資料庫引擎**：
  - **Microsoft SQL Server (MSSQL)**：支援本機 **Windows 驗證**免密碼登入（可直接搭配 **SSMS** 實作展示），自動處理 UTC 與本機時區轉換。
  - **MySQL 8** 與 **MariaDB 11**：可透過側邊欄自訂主機、埠號與帳密連線。
- **動態探索任意資料庫與資料表**：自動掃描伺服器上的使用者資料庫與資料表（免寫死表名或欄位）。
- **一鍵為任意表掛載時光機**：
  - **SQL Server**：一鍵開啟原生 Temporal Table (`SYSTEM_VERSIONING = ON`)。
  - **MariaDB**：一鍵開啟原生系統版本化 (`ADD SYSTEM VERSIONING`)。
  - **MySQL 8**：一鍵建立 `{表名}_tm_history` JSON 影子表並自動安裝 `INSERT / UPDATE / DELETE` 觸發器。
- **即時監控與無損救援**：
  - 開啟 **「自動監控 (3秒)」** 後，任何外部工具（如 SSMS、MySQL Workbench、後端程式）對資料表的修改都會在 3 秒內即時呈現在網頁上。
  - 支援任意時間點雙表對照與三色差異標記（🔴 被刪除、🟡 欄位變更、🟢 後續新增）。
  - 支援依主鍵執行 **單筆還原**、**全表一鍵還原**（含 SQL Server `IDENTITY` 自動編號欄位自動處理）與 **單筆完整異動時間軸** 查詢。

*(若要重現本專題完整的「四種架構效能評測與單元測試」，請往下參考第五～六章)*

---

## 📑 目錄
1. [專題目標與研究問題](#一專題目標與研究問題)
2. [系統架構圖與設計方案](#二系統架構圖與設計方案)
3. [四種設計核心原理與實作細節](#三四種設計核心原理與實作細節)
4. [專案目錄結構](#四專案目錄結構)
5. [環境準備與安裝步驟](#五環境準備與安裝步驟)
6. [驗證與執行指南 (測試、量測、介面)](#六驗證與執行指南)
7. [效能評測成果與優缺點比較摘要](#七效能評測成果與優缺點比較摘要)
8. [各設計已知限制與技術說明](#八各設計已知限制與技術說明)
9. [口試問答 (FAQ) 重點指引](#九口試問答-faq-重點指引)

---

## 一、專題目標與研究問題

在傳統的資料庫設計中，`UPDATE` 會直接覆寫原有資料，`DELETE` 會將資料列自磁碟中移除，導致歷史資訊永久滅失。
本專題針對「銀行帳戶」情境（包含帳戶 ID、姓名、餘額、異動微秒時間戳），實作並全方位比較四種歷史保留設計：
- **設計 A：定期快照 (Periodic Snapshot)**
- **設計 B：觸發器 ＋ 歷史表 (Trigger + History Table)**
- **設計 C：事件溯源 (Event Sourcing ＋ 檢查點 Checkpoints)**
- **設計 D：MariaDB 原生系統版本化資料表 (WITH SYSTEM VERSIONING)**

核心目標在於回答：
1. **空間成本**：記錄歷次變更究竟需要多出多少儲存空間（MB）？
2. **寫入吞吐量**：加入歷史機制後，相較於不記錄歷史的普通表（Baseline），每秒寫入次數（TPS）下降了多少？
3. **時間點查詢延遲**：查詢「$t$ 時間點整張表」與「單一帳戶在 $t$ 的狀態」耗時多少毫秒？複合索引前後有何差異？
4. **還原代價**：將單筆、1,000 筆或整張主表回復至過去某個健康時間點，各設計的計算與 I/O 耗時為何？
5. **資料正確性**：能否保證抽樣歷史時刻與真實世界操作日誌 100% 嚴格一致？

---

## 二、系統架構圖與設計方案

```text
                               +------------------------------------------+
                               |     Streamlit 互動介面 (app.py)          |
                               |   - 歷史與當前雙表比對 (Diff 高亮)       |
                               |   - 一鍵製造災難 (誤刪Acc#88)與即時還原  |
                               |   - 單一帳戶完整審計時間軸 (Timeline)    |
                               +--------------------+---------------------+
                                                    |
                               +--------------------+---------------------+
                               |   評測腳本與單元測試 (benchmark / pytest)|
                               +--------------------+---------------------+
                                                    |
             +--------------------------------------+--------------------------------------+
             |                                      |                                      |
             v                                      v                                      v
   [ 設計 A：定期快照 ]                   [ 設計 B：觸發器 ＋ 歷史表 ]            [ 設計 C：事件溯源 ]
   - accounts_a (主表)                    - accounts_b (主表)                    - accounts_c (投影主表)
   - accounts_snapshot (快照表)           - accounts_history (歷史表)             - account_events (不可變事件)
   - 定期整表複製                         - BEFORE UPDATE/DELETE 觸發器          - account_checkpoints (檢查點)
   - 取 taken_at <= t 最近快照            - valid_from <= t < valid_to 區間查詢  - state_at(t) 逐批流式重播
             |                                      |                                      |
             +--------------------------------------+--------------------------------------+
                                                    |
                                      +-------------+-------------+
                                      | MySQL 8.0 容器 (埠號 3306)|
                                      +---------------------------+

                                      +---------------------------+
                                      | MariaDB 11 容器 (埠號 3307)|
                                      +-------------+-------------+
                                                    |
                                                    v
                                      [ 設計 D：原生系統版本化資料表 ]
                                      - accounts_sv WITH SYSTEM VERSIONING
                                      - SQL:2011 標準 FOR SYSTEM_TIME AS OF t
                                      - 引擎核心自動維護 row_start / row_end
```

---

## 三、四種設計核心原理與實作細節

### 1. 設計 A：定期快照 (Periodic Snapshot)
- **實作原理**：每次執行 `take_snapshot()` 時，將當前 `accounts_a` 的所有內容以整表複製方式寫入 `accounts_snapshot`，並給予遞增的 `snapshot_id` 與 `taken_at`。
- **查詢邏輯**：時間點查詢時，取出 `taken_at <= t ORDER BY taken_at DESC LIMIT 1` 之最新快照內容。
- **優缺點**：
  - **優點**：讀取歷史狀態極為直觀單純，查詢延遲低。
  - **缺點**：空間佔用呈 $O(S \times N)$ 階梯式暴增；無法查詢兩次快照區間內發生的微異動（粗粒度）。

### 2. 設計 B：觸發器 ＋ 歷史表 (Trigger + History Table)
- **實作原理**：
  - 主表 `accounts_b` 僅保存當前最新列。
  - 建立 `BEFORE UPDATE` 與 `BEFORE DELETE` 觸發器：在覆寫或刪除發生前，資料庫引擎在同一交易內將 `OLD` 資料寫入 `accounts_history`，並將其有效區間記錄為 `[OLD.updated_at, NEW.updated_at)`。
- **查詢邏輯**：
  - 歷史表條件：`valid_from <= t AND t < valid_to`
  - 主表條件：`updated_at <= t`
  - 兩者使用 `UNION ALL` 聯集。由於左閉右開區間在數學上完全互斥，保證任何時間點每筆帳戶最多僅命中一列。
- **優缺點**：
  - **優點**：空間極省（僅記錄異動差量 $O(U)$）；時間精度達微秒級。
  - **缺點**：觸發器在寫入路徑上額外增加了一次寫入與 SQL 直譯開銷，降低寫入吞吐量。

### 3. 設計 C：事件溯源 (Event Sourcing)
- **實作原理**：
  - 所有業務動作均轉換為不可變事件：`CREATE`（建立）、`DEPOSIT`（存款）、`WITHDRAW`（提款）、`RENAME`（更名）、`DELETE`（銷戶）。
  - 事件表 `account_events` 採 Append-only 寫入，禁止任何 Update 與 Delete。
  - 為避免隨時間膨脹重播變慢，定期建立 `account_checkpoints` 儲存基底狀態。
- **查詢邏輯**：
  - 函式 `state_at(t)`：先讀取不晚於 $t$ 的最近一次檢查點，再以 `cur.fetchmany(5000)` 分批流式重播後續事件流，演算出目標時刻的精確餘額。
- **優缺點**：
  - **優點**：保留最完整的商業語義與審計意圖（Audit Trail），天然具備不可篡改性。
  - **缺點**：全表重播的 CPU 與 I/O 計算開銷龐大，查詢與還原延遲最高。

### 4. 設計 D：MariaDB 原生系統版本化 (WITH SYSTEM VERSIONING)
- **實作原理**：
  - 利用 MariaDB 11 支援的 SQL:2011 時態規格，宣告 `WITH SYSTEM VERSIONING`。
  - 引擎層在每列底層隱式附加 `row_start` 與 `row_end` 交易時間戳。
- **查詢邏輯**：
  - 使用 SQL 標準語法：`SELECT ... FROM accounts_sv FOR SYSTEM_TIME AS OF %s`。
  - 檢視歷史版本：`SELECT ..., ROW_START, ROW_END FROM accounts_sv FOR SYSTEM_TIME ALL`。
- **優缺點**：
  - **優點**：C++ 儲存引擎底層原生維護，無觸發器直譯開銷，寫入吞吐量極高；語法優雅簡潔。
  - **缺點**：強依賴特定資料庫（MariaDB/SQL Server/Oracle），MySQL 8 原生不支援；時間戳記嚴格受限於系統提交時間，無法自訂或回填業務歷史時間。

---

## 四、專案目錄結構

```text
db_timemachine/
│
├── timemachine/                 # 核心套件模組
│   ├── __init__.py              # 套件匯出介面
│   ├── base.py                  # 抽象基底類別 (BaseTimeMachine)
│   ├── db.py                    # 資料庫連線工廠 (時區同步與中文錯誤診斷)
│   ├── snapshot.py              # 設計 A：定期快照實作
│   ├── trigger_history.py       # 設計 B：觸發器歷史表實作
│   ├── event_sourcing.py        # 設計 C：事件溯源與分批重播實作
│   ├── native_temporal.py       # 設計 D：MariaDB 原生時態實作
│   ├── generic_monitor.py       # 通用監控模組 (支援任意 MySQL / MariaDB 表)
│   └── mssql_monitor.py         # SQL Server (MSSQL) 通用監控與原生時態模組
│
├── tests/                       # Pytest 單元測試套件
│   ├── conftest.py              # 測試組態與資料庫 Fixtures
│   ├── test_trigger_history.py  # 設計 B 完整功能測試
│   ├── test_snapshot.py         # 設計 A 週期與快照測試
│   ├── test_event_sourcing.py   # 設計 C 重播與檢查點測試
│   ├── test_native_temporal.py  # 設計 D 原生時態查詢測試
│   ├── test_cross_designs.py    # 跨設計結果一致性比對測試
│   ├── test_generic_monitor.py  # MySQL 通用即時監控測試
│   └── test_mssql_monitor.py    # SQL Server 通用即時監控測試
│
├── figures/                     # Matplotlib 效能評測長條圖產出目錄
│   ├── 01_storage_size.png      # 儲存空間佔用比較
│   ├── 02_write_throughput.png  # 寫入吞吐量 (TPS) 比較
│   ├── 03_query_latency.png     # 時間點查詢延遲比較 (含索引前後)
│   └── 04_restore_latency.png   # 歷史還原耗時比較 (單筆/1000筆/整表)
│
├── app.py                       # 單一檔案 Streamlit 視覺化互動介面
├── benchmark.py                 # 嚴格效能評測與災難還原量測腳本
├── init_db.py                   # 資料庫連線診斷與 Schema 自動套用工具
├── schema_mysql.sql             # MySQL 結構與觸發器定義 (Baseline, A, B, C)
├── schema_mariadb.sql           # MariaDB 結構定義 (Baseline-MariaDB, D)
├── docker-compose.yml           # MySQL 8 (3306) 與 MariaDB 11 (3307) 容器設定
├── requirements.txt             # Python 依賴套件清單
├── results.csv                  # 實驗數據原始 CSV
├── results.md                   # 格式化 Markdown 實驗報表
├── .env.example                 # 環境變數範本檔
├── .env                         # 本地環境變數設定檔 (不寫死密碼)
└── README.md                    # 本專案完整說明文件
```

---

## 五、環境準備與安裝步驟

### 1. 系統環境需求
- **作業系統**：Windows / macOS / Linux
- **Python**：Python 3.10 以上（本專案已在 Python 3.13 驗證通過）
- **容器環境**：Docker Desktop

### 2. 啟動資料庫容器
專案提供完整的 `docker-compose.yml`，包含 MySQL 8.0（埠號 3306）與 MariaDB 11.4（埠號 3307）：

```powershell
docker compose up -d
```
*(若為舊版 Docker，請使用 `docker-compose up -d`)*

### 3. 安裝 Python 相依套件
建議使用虛擬環境（venv）：

```powershell
python -m venv venv
.\venv\Scripts\activate   # Windows PowerShell
pip install -r requirements.txt
```

### 4. 執行環境診斷與結構初始化
執行內建的診斷工具，一鍵確認雙資料庫連線並自動載入 SQL Schema：

```powershell
python init_db.py
```

---

## 六、驗證與執行指南

### 1. 執行單元測試 (Pytest)
執行全套測試，驗證 19 項測試指標（包含四種設計之新增、更新、刪除回溯、還原與跨設計 100% 數據一致性）：

```powershell
pytest -v tests/
```

### 2. 執行效能量測 (Benchmark)
評測腳本支援命令列參數調整規模。量測結束後會自動生成 `results.csv`、`results.md` 與 4 張圖表至 `figures/`：

```powershell
# 檢視參數說明
python benchmark.py --help

# 快速驗證測試 (1,000 筆帳戶、100 步變動、重複 2 次)
python benchmark.py -n 1000 -k 2 --history-steps 100 --snapshots 10 -r 2

# 標準完整口試評測 (10,000 筆帳戶、300 步變動、15 次快照、重複 5 次)
python benchmark.py -n 10000 -k 5 --history-steps 300 --snapshots 15 -r 5
```

### 3. 啟動 Streamlit 視覺化互動介面
啟動後在瀏覽器開啟 `http://localhost:8501`：

```powershell
streamlit run app.py
```

#### 介面操作特點：
1. **側邊欄切換架構**：自由切換設計 A、B、C、D，即時檢視各設計相關資料表的儲存空間（MB）。
2. **一鍵製造災難**：點擊「💥 一鍵製造情境 (誤刪與篡改)」，系統會自動建立測試帳戶 #88（張小明，$50,000），記錄安全時刻後立即將其刪除，並篡改 #89 的餘額。
3. **雙表 Diff 視覺化**：左側呈現歷史時間點狀態，右側呈現當前主表狀態，系統自動以紅、黃、綠三色高亮標示遭刪除或異動之資料列。
4. **一鍵時光還原**：點擊「🔄 執行單筆還原」或「🚀 執行全表一鍵還原」，即刻在資料庫中完成時光倒帶救援！
5. **歷史時間軸 (Timeline)**：輸入任意帳戶 ID，即可調閱該帳戶歷次變更的完整時間軸。

---

## 七、效能評測成果與優缺點比較摘要

### 實測效能總表 (1,000 筆帳戶實測範例)

| 設計方案 | 空間成本 (MB) | 寫入吞吐 (TPS) | 單筆查詢 (有/無索引 ms) | 全表查詢 (有/無索引 ms) | 還原1筆 (ms) | 還原1000筆 (實測 ms) | 還原整表 (ms) | 時間點一致率 (%) | 逐筆準確率 (%) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Baseline (MySQL)** | **0.078** | 70.63 ± 1.75 | 2.25 / N/A | 11.66 / N/A | N/A | N/A | N/A | N/A | N/A |
| **Baseline (MariaDB)**| 0.081 | 231.40 ± 8.10 | 2.10 / N/A | 8.50 / N/A | N/A | N/A | N/A | N/A | N/A |
| **設計 A (定期快照)** | 0.281 | 69.81 ± 1.01 | 2.57 / N/A | 9.28 / N/A | 3.93 | 4223.41 | 12.95 | 如實反映快照間隔 | 92.40% |
| **設計 B (觸發器+歷史表)** | 0.578 | 62.30 ± 2.47 | 2.54 / 2.59 | 13.80 / 14.19 | **3.05** | **2817.01** | **15.43** | **100.00%** | **100.00%** |
| **設計 C (事件溯源)** | 0.656 | 56.48 ± 4.44 | 4.19 / 4.82 | 38.13 / 37.25 | 8.81 | 7340.40 | 75.76 | **100.00%** | **100.00%** |
| **設計 D (MariaDB原生)** | 0.250 | **219.78 ± 9.25** | **2.17** / N/A | **8.91** / N/A | 3.19 | 3244.89 | 18.44 | **100.00%** | **100.00%** |

### 各設計優缺點矩陣

| 設計架構 | 空間成長複雜度 | 寫入開銷 | 點查延遲 | 全表查延遲 | 還原代價 | 適用場景 |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| **設計 A (快照)** | 極高 $O(S \times N)$ | 極低 | 極低 | 低 | 中等 | 資料異動極低、按日/按月備份之報表庫 |
| **設計 B (觸發器)** | 低 $O(U)$ | 中等 | 低 | 中等 | **極低** | 既有 MySQL 系統無痛升級微秒級審計 |
| **設計 C (事件溯源)**| 中等 $O(E)$ | 低至中 | 中等 | **高** | 高 | 金融清結算、高頻變更且重視業務意圖之核心領域 |
| **設計 D (原生時態)**| **極低** | **極低** | **極低** | **極低** | 低 | 允許選用 MariaDB、重視超高效能時態追蹤之現代系統 |

---

## 八、各設計已知限制與技術說明

1. **設計 D（MariaDB 原生時態）無法回填歷史時間**：
   - MariaDB 的 `SYSTEM VERSIONING` 強制由交易提交時的核心伺服器時鐘賦予 `row_start` 與 `row_end`，應用層無法人為偽造過去時間戳。本專題採用「真實操作流（注入微秒延遲）＋ 同步記錄真實歷史」之測試方法進行公平評測。
2. **設計 A（定期快照）的粗粒度限制**：
   - 快照僅能取得「不晚於目標時刻的最近一次快照」。在嚴格核對（200 個帳戶全數相等才算通過）的標準下，兩次快照區間內的任何微異動均無法反映，時間點一致率如實揭露此限制。
3. **設計 C（事件溯源）的全表重播計算負載**：
   - 重播計算複雜度隨事件數量 $O(E)$ 上升。為防止 Python 記憶體溢位，實作中改採 `cur.fetchmany(5000)` 流式重播與 Checkpoint 剪枝。
4. **雙資料庫引擎對照公平性**：
   - 設計 D 跑在 MariaDB 11，設計 A、B、C 跑在 MySQL 8。評測時特別引入 **Baseline (MariaDB)** 作為對照組，證明設計 D 的高寫入吞吐量源於 MariaDB 引擎的原生時態優化，而非跨引擎的不對等比較。
5. **時區與隔離層級防護**：
   - 所有時間戳記一律由 Python 端的 `datetime.now()` 微秒時間統一指派，連線時自動執行 `SET time_zone` 同步 Session 時區，消除 Docker 容器預設 UTC 導致的時間偏移。
   - 所有查詢函式末尾明確執行 `conn.commit()`，打破 InnoDB `REPEATABLE READ` 鎖定之舊視圖，確保即時讀取最新版本。

---

## 九、口試問答 (FAQ) 重點指引

**Q1：為什麼設計 B 的時間範圍條件是 `valid_from <= t AND t < valid_to`（左閉右開）？**  
> **回答重點**：避免時間點歧義。若使用閉區間，在版本切換的精確瞬間會同時匹配到舊版本與新版本。採用左閉右開保證在任何時間點 $t$，歷史表中最多只有一筆符合條件；且當 $t \ge \text{current.updated\_at}$ 時僅命中主表，兩者以 `UNION ALL` 聯集時天然互斥。

**Q2：還原操作（Restore）如何確保資料庫審計追蹤（Audit Trail）的完整性？**  
> **回答重點**：時光機的還原不是在資料庫底層竄改歷史，而是將歷史資料作為「新版本」寫入主表。例如設計 B 還原時執行的 `UPDATE` 或 `INSERT` 會再次觸發觸發器，將當前狀態記錄入歷史表後再切換為還原值。時間永遠向前推進，歷史軌跡完全保留。

**Q3：為什麼 MySQL 8 查詢儲存空間前必須執行 `SET SESSION information_schema_stats_expiry = 0`？**  
> **回答重點**：MySQL 8 為了加速 `information_schema` 的查詢，預設會快取資料表的統計資訊長達 24 小時。若不將快取過期時間設為 0 並執行 `ANALYZE TABLE`，查詢到的 `data_length` 與 `index_length` 將無法即時反映剛寫入的巨量資料。

---

### 🎓 專題結語
本專案完整落實了關聯式資料庫四種變更歷史設計的架構比較，從底層觸發器、不可變事件流、原生時態引擎到頂層 Streamlit 互動視覺化，提供了完整的理論依據與扎實的工程實踐。

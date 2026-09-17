# tw-cdc-influenza-news-archive

蒐集 **2023-01-01 起至今**、與「疾管署／疾病管制署／流感／influenza」相關的臺灣新聞，
保存 **發布日期、標題、完整內文、原始網址**，並輸出可稽核的覆蓋率報告。

---

## ⚠️ 關於「完整性」，請先讀這一段

本工具產出的不是「網路上所有相關新聞」。它產出的是：

> **在指定查詢、日期範圍、搜尋供應商及執行時間下可發現的新聞集合。**

這句話同時寫在 `coverage_report.json`、`run_manifest.json` 以及每一次 `crawl` 的
主控台輸出裡（原文見 `models.COMPLETENESS_STATEMENT`）。原因有四個，都無法靠寫程式解決：

1. **搜尋供應商的索引不等於網際網路。** Google News 沒有收錄的新聞，任何 Google 系
   的 API 都找不到。
2. **結果有上限。** 供應商對單一查詢會截斷結果。本工具的因應方式是把月切成週、週切
   成日（見下方「日期切片」），但切到「日」還是撞到上限時就無法再細分，此時該月份會
   被標記為不完整，而不是被當成完整。
3. **有些網站不讓我們讀。** 遇到 robots.txt 禁止、403、429、CAPTCHA、登入牆或付費牆
   時，本工具**停止**請求該來源並記錄原因，不會嘗試繞過。這些 URL 會出現在
   `failed_urls.jsonl`，對應月份會被標記為不完整。
4. **查詢詞決定了視野。** 換一組查詢就會得到不同的集合。查詢詞放在設定檔
   （`configs/default.yaml`）裡，本來就是要讓你調整、並在報告中留下紀錄。

`coverage_report.json` 會為 **2023-01 到你指定結束月份的每一個月**都輸出一列狀態
（`complete` / `INCOMPLETE` / `NOT ATTEMPTED`），讓缺口是看得見的，而不是隱形的。

---

## 安裝（Windows）

```powershell
git clone <repository-url>
cd tw-cdc-influenza-news-archive
powershell -ExecutionPolicy Bypass -File .\setup.ps1
Copy-Item .env.example .env
notepad .env       # 填 CRAWLER_CONTACT_EMAIL（必填）與 SERPAPI_API_KEY（選填）
.\.venv\Scripts\python.exe -m tw_cdc_influenza_news doctor
```

`setup.ps1` 會：檢查 Python 3.12 → 在 repository 內建立 `.venv` → 依
`requirements.lock` 安裝固定版本 → 安裝 Playwright Chromium。
它**不動系統 Python、不需要系統管理員權限**，任一步失敗就立即停止並印出可行動的錯誤訊息。

不想裝瀏覽器可加 `-SkipPlaywright`，並把設定檔的 `extraction.playwright.enabled` 設為
`false`。

### 安裝（Linux / macOS）

```bash
git clone <repository-url>
cd tw-cdc-influenza-news-archive
./setup.sh                 # 或 ./setup.sh --skip-playwright
cp .env.example .env
$EDITOR .env
./.venv/bin/python -m tw_cdc_influenza_news doctor
```

### `doctor` 檢查什麼

Python 版本、是否在虛擬環境、必要套件、設定檔是否合法、輸出目錄可否寫入、
API key **是否存在**（絕不印出內容）、Playwright Chromium 是否可啟動、SQLite schema
版本、是否有未完成的 run、以及實際生效的日期範圍。
`--offline` 會跳過所有需要網路的檢查。

---

## 使用

Windows 可以用 `.\run.ps1 <command>`（會自動載入 `.env` 到該次行程，且不回顯任何值）；
以下一律寫成完整指令，比較不會誤會。

### 首次小範圍試跑（建議）

```powershell
.\.venv\Scripts\python.exe -m tw_cdc_influenza_news crawl `
  --start-date 2024-01-01 `
  --end-date 2024-01-31 `
  --provider serpapi-google-news
```

### 全期間執行

```powershell
.\.venv\Scripts\python.exe -m tw_cdc_influenza_news crawl `
  --start-date 2023-01-01 `
  --end-date 2026-12-31 `
  --provider serpapi-google-news
```

結束日會自動夾到 **Asia/Taipei 的今天**；不會對未來日期送出查詢。

### 續跑

```powershell
.\.venv\Scripts\python.exe -m tw_cdc_influenza_news resume --run-id RUN_ID
```

已完成的日期切片不會重跑，已成功抓取的 URL 不會重抓，可重試的錯誤會重試。
`--run-id` 省略時續跑最近一次 run；`list-runs` 可以列出所有 run。

強制重抓：

```powershell
# 指定 URL
... resume --run-id RUN_ID --refetch-url https://news.example.tw/a

# 指定日期範圍（半開區間 [from, to)）
... resume --run-id RUN_ID --refetch-from 2024-01-01 --refetch-to 2024-02-01
```

### 匯入已知 URL（不需要 API key）

```powershell
.\.venv\Scripts\python.exe -m tw_cdc_influenza_news import-urls urls.jsonl
```

接受 `.jsonl`、`.json`、`.txt` 三種格式（範例見 `tests/fixtures/providers/`）。
匯入的 URL 走**完全相同**的抓取、擷取、去重與輸出流程。

### 輸出與報告

```powershell
.\.venv\Scripts\python.exe -m tw_cdc_influenza_news export --run-id RUN_ID --format json
.\.venv\Scripts\python.exe -m tw_cdc_influenza_news report --run-id RUN_ID
```

`export` 省略 `--run-id` 會匯出整個資料庫（多次續跑累積的成果），這通常才是你要的。

---

## 三個 discovery provider

| provider | 需要 API key | 完整性 | 說明 |
| --- | --- | --- | --- |
| `serpapi-google-news` | 是 | 有日期切片，可觀測截斷 | **建議主要模式**。SerpApi 的 `engine=google_news`，`gl=tw`、`hl=zh-TW`、依日期排序，原始回應會存檔（已移除 API key）。配額或授權失敗時 **fail closed**。 |
| `serpapi-google-search-news-tab` | 是 | 同上 | `engine=google&tbm=nws`，一般搜尋的 News 分頁。預設關閉。與上者**合併**而非取代，兩者結果不假設相同。 |
| `google-news-rss` | 否 | **best effort，非完整性保證** | Google News RSS 無總數、無分頁游標，短結果與被截斷的結果無法區分。低頻率請求、原始 XML 快取；一偵測到 consent 頁、CAPTCHA、403/429 就**停止**。 |
| `manual-urls` | 否 | 取決於你的清單 | 由 `import-urls` 驅動，不做搜尋。 |

沒有 API key 時：`manual-urls` 與 `google-news-rss`（明確標示 best-effort）仍可運作。

---

## 日期切片

所有切片都是**半開區間 `[start, end)`**，因此每一天剛好屬於一個切片，邊界日期不會重複也不會遺漏。

```
月 → （疑似截斷）→ 週 → （仍疑似截斷）→ 日 → 無法再切，標記 completeness_warning
```

「疑似截斷」的判斷：結果數達到 `slicing.max_results_per_slice × result_limit_ratio`
（預設 100 × 0.9 = 90），或供應商回報還有分頁但已達 `max_pages_per_slice`。

**每個 adapter 自行轉換日期參數**，不假設各家語法相同。實際差異：

- SerpApi 用 Google 的 `tbs=cdr:1,cd_min,cd_max`，**兩端皆含**，所以上界要用
  `last_day`（切片的 `end` 減一天）。
- Google News RSS 用查詢字串裡的 `after:` / `before:`，`after:` 含、`before:` 不含，
  剛好對應我們的 `[start, end)`，所以直接帶入切片邊界。

把其中一種轉換複製到另一個 adapter，會整整平移一天份的新聞。

---

## 輸出檔案

預設寫在 `data/output/`（可在設定檔改）：

| 檔案 | 內容 |
| --- | --- |
| `articles.jsonl` | 一行一篇，適合串流處理 |
| `articles.json` | 同樣內容的陣列，適合直接讀進來 |
| `coverage_report.json` | 每個 slice、每個月的覆蓋率統計 |
| `coverage_report.md` | 人類可讀版，依年月列出缺口 |
| `failed_urls.jsonl` | 每一個抓不到內文的 URL 及原因 |
| `run_manifest.json` | config hash、版本、起訖時間、provider、統計 |

每筆文章的欄位（完整定義見 `models.Article`）：

```json
{
  "record_id": "canonical URL 的 sha256，跨次執行穩定",
  "published_at": "2024-01-15T08:30:00+08:00",
  "published_date": "2024-01-15",
  "published_at_raw": "原始日期文字",
  "date_source": "json_ld",
  "date_confidence": "high",
  "title": "新聞標題",
  "content": "清理後的完整正文",
  "source_name": "新聞媒體名稱",
  "authors": [],
  "url": "搜尋結果取得的網址",
  "final_url": "跟完 redirect 的網址",
  "canonical_url": "canonicalized URL",
  "language": "zh-TW",
  "matched_queries": [],
  "matched_keywords": [],
  "discovery_providers": [],
  "discovery_evidence": [],
  "first_discovered_at": "ISO 8601",
  "last_checked_at": "ISO 8601",
  "extraction_method": "trafilatura",
  "content_sha256": "sha256",
  "syndication_cluster_id": null,
  "possible_duplicate_of": [],
  "status": "success",
  "error_code": null,
  "error_message": null
}
```

規則：全 UTF-8（不逃脫中文）；時間一律 ISO 8601；JSON **不含 NaN／Infinity**（序列化時
`allow_nan=False`）；原始時區語意保留（`published_at` 是發布者的時區，`published_date`
是 Asia/Taipei 的日曆日，兩者在接近午夜時會不同，都值得保留）；**不確定的日期就是
`null`，不會猜一個填進去**；API key、cookie、authorization header 不會進入輸出或 log。

### 日期來源優先序

`datePublished` (JSON-LD) → OpenGraph `article:published_time` → `<time datetime>` →
頁面文字中明確標示的發布時間 → 搜尋供應商提供的日期。

**搜尋供應商的日期永遠不會覆蓋新聞頁自己宣告的發布日期**——供應商給的常常是索引時間，
讓它覆蓋會把文章悄悄搬到別的月份，之後在覆蓋率報告上看起來像是無法解釋的異常。
供應商日期的 `date_confidence` 一律是 `low`。

支援民國紀年（`民國113年2月29日` → `2024-02-29`）與中文年月日格式。

---

## 去重與通訊社稿件

依序判斷，確定性由高到低：

1. canonical URL
2. 移除追蹤參數後的 normalized URL
3. 正規化標題 + 發布日期
4. 正文 SHA-256
5. 高相似標題 → **只標記** `possible_duplicate_of`，**絕不自動刪除**

**通訊社稿件不會被丟掉。** 同一段正文出現在不同網域時，那是多家媒體轉載的同一則稿件，
每一家的 URL 都是真實、可獨立引用的出處。這種情況會給一個共同的
`syndication_cluster_id`，**所有副本都保留**。

同一篇新聞被多個 query 或 provider 找到時，只會有一筆 canonical article，
`matched_queries` 與 `discovery_providers` 合併，所有 discovery evidence 都保存。

---

## 存取政策（重要）

目標是**降低觸發限制**，不是規避限制。預設值：

- `MAX_CONCURRENCY=2`、每網域每分鐘 6 次請求（**各網域獨立計算**）
- timeout 30 秒、最多重試 3 次、指數退避加 jitter
- 遵守 `Retry-After`
- robots.txt 會讀取並快取（預設 24 小時）
- User-Agent 可辨識，含專案名稱與**你的聯絡信箱**

偵測到 CAPTCHA、unusual traffic、access denied、403、429、robots.txt 禁止、
需要登入或付費牆時：**記錄事件並停止該 provider 或網域**，把缺口寫進覆蓋率報告。

本專案**沒有也不會有**：CAPTCHA 破解或 solver、代理 IP 輪替、住宅代理、Tor、
瀏覽器指紋偽裝、stealth plugin、undetected-chromedriver、登入狀態竊取，或任何
規避存取限制的機制。`tests/test_blocking.py` 有一項測試會掃描整個套件，
確認這些名稱不存在。

Playwright 只在**有明確證據顯示頁面需要 JavaScript**時才啟用（app shell 且幾乎抓不到
文字、或頁面自己寫著需要 JavaScript），使用明確的 locator 與等待條件，不用固定 sleep，
每次都開全新的空白 context，**不使用也不保存任何個人 Chrome profile 或 cookie**。

---

## 斷點續跑

SQLite（`data/state/archive.sqlite3`）是執行狀態與 canonical store。
**每處理完一個 URL 就立刻寫入狀態**，不是整批結束才寫，所以中途按 Ctrl-C 最多只損失
當下那一個 URL 的工作。

URL 狀態：`discovered` → `queued` → `fetching` → `success` / `retryable_error` /
`permanent_error` / `blocked` / `content_unavailable`。

被中止後卡在 `fetching` 的列會在下次執行時自動退回 `queued`（它們沒有寫出任何東西，
重做是安全的）。每次 run 都保存 config hash、程式版本、起訖時間、provider 與統計。

---

## 設定

設定檔位置依序：`--config` 參數 → `NEWS_CONFIG_PATH` 環境變數 → `configs/default.yaml`。
所有路徑以 repository root 為基準（或設定為絕對路徑），沒有任何開發機器的硬編碼路徑。

查詢詞放在設定檔的 `queries:`，不在擷取邏輯裡。預設六組：

```yaml
- '"疾管署" "流感"'
- '"疾病管制署" "流感"'
- '"疾管署" influenza'
- '"疾病管制署" influenza'
- '疾管署 流感'
- '疾病管制署 流感'
```

常用的調整位置：`slicing.max_results_per_slice`、`access.requests_per_domain_per_minute`、
`extraction.min_content_chars`、`coverage.*_threshold`、`deduplication.tracking_params`。

`.env` 只放三個變數（範本見 `.env.example`，只含 placeholder）：
`SERPAPI_API_KEY`、`CRAWLER_CONTACT_EMAIL`、`NEWS_CONFIG_PATH`。
`.env`、`.venv`、API 回應快取、SQLite、輸出、瀏覽器 profile、cookies、logs
都在 `.gitignore` 裡。

---

## 測試

離線、以 fixture 驅動，不連網、不開瀏覽器、不讀真的 API key。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

單獨跑某一類：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_date_slicing.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_deduplication.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_blocking.py tests/test_security.py -v
```

涵蓋：月／週／日切片邊界、2024 閏年、2026 未結束時的有效結束日、時區、canonical URL、
UTM 清理、同一文章由不同 query 發現、syndication clustering、JSON schema、中斷後續跑、
403／429／CAPTCHA／robots disallow、正文缺失、API key 不得出現在 log、JSON 不得含 NaN。

---

## 疑難排解

| 症狀 | 處理 |
| --- | --- |
| `doctor` 說找不到 Python 3.12 | 安裝 3.12，或 `setup.ps1 -PythonExe "C:\...\python.exe"` |
| `requirements.lock` 安裝失敗 | `pip install -e ".[dev]"` 後 `pip freeze --exclude-editable > requirements.lock` 重新產生 |
| Chromium 啟動失敗 | `.\.venv\Scripts\python.exe -m playwright install chromium`；或關閉 `extraction.playwright.enabled` |
| provider 立刻停止，錯誤 `provider_quota_exhausted` | SerpApi 配額用完。這是**故意** fail closed，避免把帳務問題偽裝成覆蓋率缺口 |
| 某網域整段消失 | 看 `coverage_report.md` 的 Stopped domains，以及 `failed_urls.jsonl` 的原因 |
| 很多 `content_unavailable` | 通常是該站需要 JavaScript 或有付費牆；檢查 `failed_urls.jsonl` 的 `error_code` |

---

## 目錄結構

```
src/tw_cdc_influenza_news/
  cli.py  config.py  models.py  database.py  orchestrator.py
  dates.py  urls.py  errors.py  ratelimit.py  logging_utils.py
  discovery/    serpapi_google_news.py  google_news_rss.py  manual_urls.py
  fetching/     http_client.py  robots.py  playwright_fallback.py  pipeline.py
  extraction/   extractor.py  dateparse.py
  deduplication/dedupe.py
  reporting/    coverage.py  export.py
configs/default.yaml
data/{raw,state,output}/        # 內容不進 Git，只留 .gitkeep
tests/{fixtures,...}
scripts/{setup.ps1,setup.sh,run.ps1}
```

## 授權與使用須知

請遵守各新聞網站的使用條款與著作權規定。本工具保存的內文供研究與分析使用；
重新發布新聞全文通常需要著作權人授權。`CRAWLER_CONTACT_EMAIL` 請填真實信箱，
讓網站營運者在需要時能聯絡到人。

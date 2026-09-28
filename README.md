# SQL Stepper

把一段 SQL 拆成它**實際的執行順序**，一步一步、一列一列地看中間結果。

```text
FROM → JOIN → WHERE → GROUP BY → 聚合 → HAVING → 視窗函數 → QUALIFY → SELECT → DISTINCT → ORDER BY → LIMIT
```

## 啟動

```bash
cd sql-stepper
uv run sqlstepper                 # 記憶體資料庫 + 範例資料表，自動開啟瀏覽器
uv run sqlstepper --db my.duckdb  # 直接操作自己的 DuckDB 檔案（不會載入範例）
uv run sqlstepper --port 9000 --no-browser
```

## 功能

- **逐步、逐列動畫**：每一步都顯示輸入與輸出；JOIN／SELECT／ORDER BY 會標出每一列是由哪些來源列產生的，
  WHERE／HAVING／QUALIFY 會逐列標上 TRUE／FALSE／NULL。暫停時，滑鼠移到任一列也會顯示它的來源。
- **斷點**：點行號設定行斷點（F9），或點執行計畫中步驟前的圓點設定步驟斷點。
  播放到斷點時，會在執行該步驟之前暫停，接著可以用「↧ 下一列」（F8）逐列前進。
- **完整語法**：JOIN（INNER／LEFT／RIGHT／FULL／CROSS／SEMI／ANTI／ASOF／POSITIONAL／LATERAL）、
  子查詢（包含相關子查詢）、CTE、遞迴 CTE（逐輪展開）、GROUP BY（ROLLUP／CUBE／GROUPING SETS）、
  視窗函數、QUALIFY、DISTINCT ON、UNION／INTERSECT／EXCEPT、PIVOT，以及 INSERT／UPDATE／DELETE 的前後差異。
  無法逐步拆解的語法會退回「只顯示結果」，並註明原因。
- **匯入資料**：CSV、TSV、Parquet、JSON／JSONL、XLSX、DuckDB、SQLite（拖放到左側即可）。
- **試跑模式**（預設開啟）：在資料庫副本上執行，DML／DDL 不會保留。

| 快捷鍵 | 動作 |
| --- | --- |
| Ctrl+Enter | 執行（從頭播放） |
| F5 | 繼續／暫停 |
| F10 | 下一步 |
| F8 | 下一列 |
| F9 | 切換目前行的斷點 |

## 運作方式

後端用 [sqlglot](https://github.com/tobymao/sqlglot) 解析 SQL，再把查詢拆成一連串「累加」的查詢，
每一步都交給 [DuckDB](https://duckdb.org/) 真正執行，所以中間結果完全遵照 DuckDB 的語意。
每個資料來源會多帶一個隱藏的列編號（`__rid_n`），用來追蹤每一列從哪裡來。
每次執行都會檢查「逐步拆解的最後結果」與「直接執行原始 SQL 的結果」是否一致，不一致時會在畫面上警告。

程式結構：

| 檔案 | 內容 |
| --- | --- |
| `src/sqlstepper/engine.py` | SQL 拆解與逐步執行 |
| `src/sqlstepper/db.py` | DuckDB 連線、範例資料、檔案匯入 |
| `src/sqlstepper/server.py` | 本機 HTTP server（只用標準函式庫） |
| `src/sqlstepper/static/` | 前端（純 HTML／CSS／JS，不需要網路） |
| `tests/` | 語法語料庫、語意、匯入／API、瀏覽器端對端測試 |

## 測試

```bash
uv run pytest                                  # 全部（瀏覽器測試需要 Edge；沒有會自動跳過）
uv run pytest tests/test_corpus.py             # 約 170 條語法案例，逐一與 DuckDB 直接執行結果比對
SQLSTEPPER_SHOTS=shots uv run pytest tests/test_e2e.py   # 另外存下每個情境的截圖
uv run python scripts/smoke.py "SELECT ..."    # 在終端機印出某段 SQL 的步驟大綱
```

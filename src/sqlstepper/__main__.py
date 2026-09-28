"""Command line entry: `uv run sqlstepper` or `python -m sqlstepper`."""

import argparse
import threading
import webbrowser

from .db import Session
from .server import serve


def main() -> None:
    ap = argparse.ArgumentParser(prog="sqlstepper", description="逐步視覺化 SQL 的執行過程")
    ap.add_argument("--db", default=":memory:", help="要開啟的 DuckDB 檔案（預設為記憶體資料庫）")
    ap.add_argument("--no-samples", action="store_true", help="不要載入範例資料表")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="不要自動開啟瀏覽器")
    args = ap.parse_args()

    # Opening a user's own database file should not silently add sample tables to it.
    load = not args.no_samples and args.db == ":memory:"
    httpd = serve(Session(args.db, load_samples=load), port=args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"SQL Stepper 已啟動：{url}（按 Ctrl+C 結束）")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已結束")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

import json
import sqlite3
import threading
import urllib.error
import urllib.request

import duckdb
import pytest

from sqlstepper.db import Session, sanitize_name, to_json_value
from sqlstepper.engine import Tracer
from sqlstepper.server import serve


# ---- imports ---------------------------------------------------------------
def fresh():
    return Session(load_samples=False)


def test_import_csv_tsv_json_parquet(tmp_path):
    s = fresh()
    (tmp_path / "sales 2024.csv").write_text("id,amount,city\n1,10.5,Taipei\n2,,Tainan\n", encoding="utf-8")
    (tmp_path / "t.tsv").write_text("a\tb\n1\tx\n", encoding="utf-8")
    (tmp_path / "e.jsonl").write_text('{"k": 1, "v": "a"}\n{"k": 2, "v": null}\n', encoding="utf-8")
    duckdb.connect().execute(f"COPY (SELECT range AS n FROM range(5)) TO '{tmp_path / 'p.parquet'}' (FORMAT parquet)")
    assert s.import_file(tmp_path / "sales 2024.csv") == ["sales_2024"]
    assert s.import_file(tmp_path / "t.tsv", "my_tsv") == ["my_tsv"]
    assert s.import_file(tmp_path / "e.jsonl") == ["e"]
    assert s.import_file(tmp_path / "p.parquet") == ["p"]
    assert s.con.execute("SELECT COUNT(*), SUM(amount) FROM sales_2024").fetchone() == (2, 10.5)
    assert s.con.execute("SELECT b FROM my_tsv").fetchone() == ("x",)
    assert s.con.execute("SELECT COUNT(v) FROM e").fetchone() == (1,)
    assert s.con.execute("SELECT SUM(n) FROM p").fetchone() == (10,)
    assert {t["name"] for t in s.tables()} == {"sales_2024", "my_tsv", "e", "p"}
    # imported tables are immediately traceable
    r = Tracer(s).run("SELECT city, SUM(amount) FROM sales_2024 GROUP BY city ORDER BY city")
    assert r["error"] is None and r["statements"][0]["pipelines"][0]["steps"][-1]["panels"][0]["total"] == 2


def test_import_duckdb_file(tmp_path):
    path = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE a AS SELECT 1 AS x; CREATE TABLE b AS SELECT 'y' AS y")
    con.close()
    s = fresh()
    assert sorted(s.import_file(path)) == ["a", "b"]
    assert s.con.execute("SELECT x FROM a").fetchone() == (1,)


def test_import_sqlite_file_including_loose_types(tmp_path):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE people (id INTEGER, name TEXT, score REAL)")
    con.executemany("INSERT INTO people VALUES (?, ?, ?)", [(1, "Ann", 9.5), (2, "Bo", None)])
    con.execute("CREATE TABLE messy (n INTEGER)")
    con.execute("INSERT INTO messy VALUES ('not a number')")  # SQLite allows this
    con.commit()
    con.close()
    s = fresh()
    assert sorted(s.import_file(path)) == ["messy", "people"]
    assert s.con.execute("SELECT name, score FROM people ORDER BY id").fetchall() == [("Ann", 9.5), ("Bo", None)]
    assert s.con.execute("SELECT n FROM messy").fetchone() == ("not a number",)


def test_import_xlsx(tmp_path):
    path = tmp_path / "book.xlsx"
    try:
        con = duckdb.connect()
        con.execute("INSTALL excel; LOAD excel")
        con.execute(f"COPY (SELECT 1 AS a, 'x' AS b) TO '{path}' (FORMAT xlsx, HEADER true)")
    except duckdb.Error as e:
        pytest.skip(f"excel extension unavailable (offline?): {e}")
    s = fresh()
    assert s.import_file(path) == ["book"]
    assert s.con.execute("SELECT a, b FROM book").fetchall() == [(1.0, "x")]


def test_import_rejects_unknown_types(tmp_path):
    (tmp_path / "x.pdf").write_bytes(b"%PDF")
    with pytest.raises(ValueError):
        fresh().import_file(tmp_path / "x.pdf")


def test_reset_and_drop():
    s = Session()
    s.con.execute("CREATE TABLE extra AS SELECT 1")
    s.drop("extra")
    assert "extra" not in {t["name"] for t in s.tables()}
    s.con.execute("CREATE VIEW v AS SELECT * FROM customers")
    s.reset_samples()
    names = {t["name"] for t in s.tables()}
    assert "v" not in names and {"customers", "orders", "employees"} <= names


def test_sanitize_and_json_values():
    import datetime as dt
    import decimal
    import uuid

    assert sanitize_name("2024 Sales-Report.csv") == "t_2024_sales_report"
    assert to_json_value(decimal.Decimal("1.50")) == "1.50"
    assert to_json_value(2**60) == str(2**60)
    assert to_json_value(float("nan")) == "nan"
    assert to_json_value(dt.date(2024, 1, 2)) == "2024-01-02"
    assert to_json_value(dt.datetime(2024, 1, 2, 3, 4)) == "2024-01-02 03:04:00"
    assert to_json_value(b"\x01") == "\\x01"
    assert to_json_value({"a": [1, None]}) == {"a": [1, None]}
    assert isinstance(to_json_value(uuid.uuid4()), str)


# ---- HTTP API -----------------------------------------------------------------
@pytest.fixture(scope="module")
def base_url():
    httpd = serve(Session(), port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(url, data=None, raw=False):
    body = data if raw else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.headers.get("Content-Type"), res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def test_static_files(base_url):
    for path, ctype in (("/", "text/html"), ("/app.js", "javascript"), ("/app.css", "text/css")):
        status, got, body = call(base_url + path)
        assert status == 200 and ctype in got and len(body) > 100
    assert call(base_url + "/../pyproject.toml")[0] == 404
    assert call(base_url + "/nope.js")[0] == 404


def test_state_trace_preview(base_url):
    status, _, body = call(base_url + "/api/state")
    state = json.loads(body)
    assert status == 200 and state["examples"] and any(t["name"] == "orders" for t in state["tables"])
    status, _, body = call(base_url + "/api/trace", {"sql": "SELECT status, COUNT(*) FROM orders GROUP BY 1"})
    trace = json.loads(body)
    assert status == 200 and trace["error"] is None
    assert [s["clause"] for s in trace["statements"][0]["pipelines"][0]["steps"]] == [
        "FROM", "GROUP BY", "AGGREGATE", "SELECT", "RESULT"]
    status, _, body = call(base_url + "/api/trace", {"sql": "SELECT * FROM nope"})
    assert status == 200 and "nope" in json.loads(body)["error"]
    status, _, body = call(base_url + "/api/preview?name=customers")
    assert status == 200 and json.loads(body)["total"] == 6


def test_import_drop_and_reset_over_http(base_url):
    status, _, body = call(base_url + "/api/import?filename=pets.csv&name=", b"name,legs\ncat,4\nbird,2\n", raw=True)
    assert status == 200 and json.loads(body)["created"] == ["pets"]
    status, _, body = call(base_url + "/api/trace", {"sql": "SELECT SUM(legs) FROM pets"})
    assert json.loads(body)["statements"][0]["pipelines"][0]["steps"][-1]["panels"][0]["rows"] == [[6]]
    status, _, body = call(base_url + "/api/drop", {"name": "pets"})
    assert status == 200 and "pets" not in {t["name"] for t in json.loads(body)["tables"]}
    assert call(base_url + "/api/drop", {"name": "pets"})[0] == 400
    assert call(base_url + "/api/import?filename=x.exe", b"MZ", raw=True)[0] == 400
    status, _, body = call(base_url + "/api/reset", {})
    assert status == 200


def test_sandbox_flag_over_http(base_url):
    sql = "CREATE TABLE made_here AS SELECT 1 AS a"
    call(base_url + "/api/trace", {"sql": sql, "sandbox": True})
    names = {t["name"] for t in json.loads(call(base_url + "/api/state")[2])["tables"]}
    assert "made_here" not in names
    call(base_url + "/api/trace", {"sql": sql, "sandbox": False})
    names = {t["name"] for t in json.loads(call(base_url + "/api/state")[2])["tables"]}
    assert "made_here" in names

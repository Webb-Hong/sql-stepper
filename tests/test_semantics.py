"""The intermediate steps themselves must be right, not just the final result."""

from conftest import check_invariants, main_pipeline


def steps_of(result, kind=None, clause=None):
    out = [s for s in main_pipeline(result)["steps"]]
    if kind:
        out = [s for s in out if s["kind"] == kind]
    if clause:
        out = [s for s in out if s["clause"] == clause]
    return out


def rows_by_id(panel):
    return dict(zip(panel["ids"], panel["rows"]))


def test_step_order_follows_logical_execution(tracer):
    r = tracer.run("""SELECT c.city, COUNT(*) AS n, RANK() OVER (ORDER BY COUNT(*) DESC) AS r
FROM orders o JOIN customers c ON c.id = o.customer_id
WHERE o.status = 'shipped'
GROUP BY c.city HAVING COUNT(*) > 0
QUALIFY r <= 5
ORDER BY n DESC LIMIT 2""")
    clauses = [s["clause"] for s in main_pipeline(r)["steps"]]
    assert clauses == ["FROM", "JOIN", "WHERE", "GROUP BY", "AGGREGATE", "HAVING", "WINDOW", "QUALIFY",
                       "SELECT", "ORDER BY", "LIMIT", "RESULT"]
    check_invariants(r)


def test_join_links_point_at_the_rows_that_were_combined(tracer):
    r = tracer.run("SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id")
    join = steps_of(r, "join")[0]
    left, right, out = join["panels"]
    lrows, rrows = rows_by_id(left), rows_by_id(right)
    assert len(join["links"]) == out["total"] == 9  # order 107 has no customer
    for row, link in zip(out["rows"], join["links"]):
        (lp, lid), (rp, rid) = link
        assert (lp, rp) == (0, 1)
        assert row[: len(left["columns"])] == lrows[lid]  # o.* columns come from that left row
        assert row[len(left["columns"]):] == rrows[rid]  # c.* columns come from that right row
        assert row[left["columns"].index("o.customer_id")] == row[len(left["columns"]) + right["columns"].index("c.id")]
    assert out["columns"][:2] == ["o.id", "o.customer_id"]


def test_left_join_keeps_unmatched_rows_with_nulls(tracer):
    r = tracer.run("SELECT * FROM customers c LEFT JOIN orders o ON o.customer_id = c.id")
    join = steps_of(r, "join")[0]
    out = join["panels"][-1]
    david = [(row, link) for row, link in zip(out["rows"], join["links"]) if row[1] == "David"]
    assert len(david) == 1
    row, link = david[0]
    assert [p for p, _ in link] == [0]  # linked to the left row only
    assert row[out["columns"].index("o.id")] is None


def test_where_marks_follow_three_valued_logic(tracer):
    r = tracer.run("SELECT * FROM customers WHERE city <> 'Taipei'")
    where = steps_of(r, clause="WHERE")[0]
    p = where["panels"][0]
    city = p["columns"].index("customers.city")
    for row, mark in zip(p["rows"], p["marks"]):
        if row[city] is None:
            assert mark is None  # NULL <> 'Taipei' is UNKNOWN, not TRUE
        else:
            assert mark == (row[city] != "Taipei")
    assert sum(m is True for m in p["marks"]) == 3


def test_group_step_colours_rows_by_key_and_aggregate_matches(tracer):
    r = tracer.run("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
    group = steps_of(r, "group")[0]["panels"][0]
    status = group["columns"].index("orders.status")
    by_group = {}
    for row, g in zip(group["rows"], group["groups"]):
        by_group.setdefault(g, set()).add(row[status])
    assert all(len(v) == 1 for v in by_group.values())  # one key per colour
    assert group["groups"] == sorted(group["groups"])  # rows are laid out group by group
    agg = steps_of(r, "aggregate")[0]
    grouped_in, out = agg["panels"]
    for row, g in zip(out["rows"], out["groups"]):
        members = [x for x, gg in zip(grouped_in["rows"], grouped_in["groups"]) if gg == g]
        assert row[out["columns"].index("COUNT(*)")] == len(members)
        assert row[0] == members[0][status]


def test_having_marks_groups(tracer):
    r = tracer.run("SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id HAVING COUNT(*) >= 2")
    having = steps_of(r, clause="HAVING")[0]["panels"][0]
    n = having["columns"].index("COUNT(*)")
    assert having["marks"] == [row[n] >= 2 for row in having["rows"]]


def test_window_step_adds_columns_and_partitions(tracer):
    r = tracer.run("SELECT name, RANK() OVER (PARTITION BY dept_id ORDER BY salary DESC) AS r FROM employees")
    w = steps_of(r, "window")[0]["panels"][0]
    assert w["highlight"] == [len(w["columns"]) - 1]
    dept = w["columns"].index("employees.dept_id")
    part = {}
    for row, g in zip(w["rows"], w["groups"]):
        part.setdefault(g, set()).add(row[dept])
    assert all(len(v) == 1 for v in part.values())


def test_project_links_back_to_source_rows(tracer):
    r = tracer.run("SELECT upper(name) AS u FROM customers WHERE id > 3")
    proj = steps_of(r, "project")[0]
    src, out = proj["panels"]
    names = rows_by_id(src)
    name_col = src["columns"].index("customers.name")
    for row, link in zip(out["rows"], proj["links"]):
        assert row[0] == names[link[0][1]][name_col].upper()


def test_distinct_marks_duplicates(tracer):
    r = tracer.run("SELECT DISTINCT status FROM orders")
    d = steps_of(r, clause="DISTINCT")[0]["panels"][0]
    seen = set()
    for row, mark in zip(d["rows"], d["marks"]):
        assert mark == (row[0] not in seen)
        seen.add(row[0])


def test_limit_offset_marks_window(tracer):
    r = tracer.run("SELECT id FROM orders ORDER BY id LIMIT 3 OFFSET 2")
    lim = steps_of(r, clause="LIMIT")[0]["panels"][0]
    assert lim["marks"] == [False, False, True, True, True] + [False] * 5
    assert steps_of(r, "result")[0]["panels"][0]["rows"] == [[103], [104], [105]]


def test_sort_links_every_row(tracer):
    r = tracer.run("SELECT name, price FROM products ORDER BY price DESC")
    sort = steps_of(r, "sort")[0]
    before, after = sort["panels"]
    rows = rows_by_id(before)
    for row, link in zip(after["rows"], sort["links"]):
        assert rows[link[0][1]] == row


def test_recursive_cte_iterations_add_up(tracer):
    r = tracer.run("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 4) SELECT * FROM r")
    rec = r["statements"][0]["pipelines"][0]
    assert rec["title"] == "遞迴 CTE r"
    produced = [s["panels"][-1]["rows"] for s in rec["steps"] if s["clause"] == "RECURSIVE"]
    assert produced == [[[1]], [[2]], [[3]], [[4]], []]
    assert rec["steps"][-1]["panels"][0]["rows"] == [[1], [2], [3], [4]]


def test_cte_and_subquery_pipelines_come_first(tracer):
    r = tracer.run("WITH t AS (SELECT * FROM orders WHERE status = 'shipped') "
                   "SELECT * FROM t WHERE customer_id IN (SELECT id FROM customers WHERE city = 'Taipei')")
    titles = [p["title"] for p in r["statements"][0]["pipelines"]]
    assert titles == ["CTE t", "WHERE 中的子查詢", "主查詢"]


def test_correlated_subquery_is_explained_not_expanded(tracer):
    r = tracer.run("SELECT name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)")
    pipes = r["statements"][0]["pipelines"]
    assert len(pipes) == 1
    assert any("相關子查詢" in n for n in pipes[0]["notes"])


def test_set_operation_links_to_both_sides(tracer):
    r = tracer.run("SELECT id FROM customers WHERE id <= 2 UNION ALL SELECT id FROM products WHERE id >= 5")
    setop = [s for s in main_pipeline(r)["steps"] if s["kind"] == "setop"][0]
    assert [l[0][0] for l in setop["links"]] == [0, 0, 1, 1]


def test_titles_use_the_users_own_text(tracer):
    r = tracer.run("select c.name\nfrom customers c\nleft join orders o on o.customer_id = c.id\n"
                   "where c.city is not null\norder by c.name desc\nlimit 3")
    titles = [s["title"] for s in main_pipeline(r)["steps"]]
    assert titles[:6] == [
        "FROM customers c",
        "LEFT JOIN orders o ON o.customer_id = c.id",
        "WHERE c.city is not null",
        "SELECT c.name",
        "ORDER BY c.name desc",
        "LIMIT 3",
    ]


def test_breakpoint_lines_map_to_clauses(tracer):
    sql = "-- comment\nSELECT 1;\n\nSELECT c.name,\n  COUNT(*)\nFROM customers c\nJOIN orders o\n  ON o.customer_id = c.id\n" \
          "WHERE o.status = 'shipped'\nGROUP BY c.name\nHAVING COUNT(*) > 1\nORDER BY 2\nLIMIT 1"
    r = tracer.run(sql)
    assert [st["line"] for st in r["statements"]] == [2, 4]
    lines = {s["clause"]: s["line"] for s in main_pipeline(r)["steps"]}
    assert lines == {"FROM": 6, "JOIN": 7, "WHERE": 9, "GROUP BY": 10, "AGGREGATE": 10, "HAVING": 11,
                     "SELECT": 4, "ORDER BY": 12, "LIMIT": 13, "RESULT": None}


def test_multiple_statements_share_state_inside_one_run(tracer, session):
    r = tracer.run("CREATE TABLE tmp_x AS SELECT 1 AS a; INSERT INTO tmp_x VALUES (2); SELECT SUM(a) FROM tmp_x")
    assert r["error"] is None
    assert main_pipeline(r)["steps"][-1]["panels"][0]["rows"] == [[3]]
    # sandboxed by default: nothing leaked into the real database
    assert "tmp_x" not in {t["name"] for t in session.tables()}


def test_dml_without_sandbox_changes_the_database(session):
    from sqlstepper.db import Session
    from sqlstepper.engine import Tracer

    s = Session()
    r = Tracer(s).run("DELETE FROM orders WHERE status = 'cancelled'", sandbox=False)
    assert r["error"] is None
    assert s.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 8


def test_update_diff_marks_changed_cells(tracer):
    r = tracer.run("UPDATE products SET price = price * 2 WHERE category = 'Furniture'")
    where, diff = main_pipeline(r)["steps"]
    assert where["clause"] == "WHERE" and sum(where["panels"][0]["marks"]) == 2
    after = diff["panels"][1]
    price = after["columns"].index("price")
    changed = [(st, ch) for st, ch in zip(after["status"], after["changed"]) if st == "changed"]
    assert changed == [("changed", [price])] * 2


def test_delete_and_insert_diffs(tracer):
    r = tracer.run("DELETE FROM orders WHERE status = 'cancelled'; INSERT INTO customers VALUES (7, 'Gina', NULL, NULL)")
    delete = r["statements"][0]["pipelines"][0]["steps"][-1]
    assert delete["panels"][0]["status"].count("deleted") == 2
    insert = r["statements"][1]["pipelines"][-1]["steps"][-1]
    assert insert["panels"][1]["status"].count("new") == 1


def test_errors_are_reported_and_stop_the_script(tracer):
    r = tracer.run("SELECT 1; SELECT * FROM no_such_table; SELECT 2")
    assert "no_such_table" in r["error"]
    assert len(r["statements"]) == 2
    assert tracer.run("SELEC 1")["error"]
    assert tracer.run("SELECT 'unterminated")["error"]
    assert tracer.run("")["statements"] == []


def test_huge_intermediate_results_are_capped(tracer):
    r = tracer.run("SELECT a.range AS x FROM range(1000) a CROSS JOIN range(200) b WHERE a.range % 7 = 0")
    assert r["error"] is None
    join = steps_of(r, "join")[0]["panels"][-1]
    assert join["total"] == 100_000 and join["overflow"] is True
    assert len(join["rows"]) == 300

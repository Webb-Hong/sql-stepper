"""Every query must trace without error, reproduce DuckDB's own result exactly,
and (unless marked loose) be decomposed step by step without fallbacks or
self-check mismatches."""

import pytest

from conftest import check_invariants, direct, main_pipeline

STRICT = [
    # --- basics ---------------------------------------------------------------
    "SELECT * FROM customers",
    "SELECT name, city FROM customers",
    "SELECT id, name AS 顧客, price * 2 AS doubled FROM products",
    'SELECT name AS "customer name" FROM customers WHERE "city" = \'Taipei\'',
    "SELECT 1 + 1 AS two, 'x' || 'y' AS s",
    "SELECT 42",
    "FROM customers",
    "FROM customers SELECT name WHERE id > 2",
    "SELECT * FROM customers WHERE city IS NULL",
    "SELECT * FROM customers WHERE city IS NOT NULL AND id BETWEEN 2 AND 5",
    "SELECT * FROM customers WHERE city IN ('Taipei', 'Kaohsiung') OR name LIKE 'D%'",
    "SELECT * FROM customers WHERE name ILIKE '%a%' AND city NOT IN ('Taichung')",
    "SELECT * FROM customers WHERE city <> 'Taipei'",  # NULL city is neither = nor <>
    "SELECT * FROM products WHERE price > 100 AND NOT (category = 'Furniture')",
    "SELECT name, CASE WHEN price > 300 THEN 'high' WHEN price > 20 THEN 'mid' ELSE 'low' END AS tier FROM products",
    "SELECT name, COALESCE(city, '(unknown)') AS city, NULLIF(city, 'Taipei') FROM customers",
    "SELECT CAST(price AS INTEGER) AS p, price::VARCHAR AS s, round(price / 3, 2) FROM products",
    "SELECT upper(name), length(name), substr(name, 1, 2), replace(name, 'a', '@') FROM customers",
    "SELECT order_date, year(order_date), date_trunc('month', order_date), order_date + INTERVAL 7 DAY FROM orders",
    "SELECT * FROM orders WHERE order_date >= DATE '2024-08-01'",
    "SELECT price * 2 AS p FROM products WHERE p > 500",  # DuckDB: alias usable in WHERE
    "SELECT * FROM customers WHERE false",
    "SELECT * FROM products WHERE regexp_matches(name, '^M')",
    "SELECT * EXCLUDE (signup_date) FROM customers",
    "SELECT * REPLACE (upper(name) AS name) FROM customers",
    "SELECT COLUMNS('^(id|name)$') FROM customers",
    "SELECT c.* FROM customers c WHERE c.id < 3",
    "SELECT [1, 2, 3] AS l, {'a': 1, 'b': 'x'} AS s, list_transform([1, 2], x -> x * 10) AS t",
    "SELECT name, string_split(name, 'a') AS parts FROM customers",
    # --- ORDER BY / LIMIT / DISTINCT ------------------------------------------------
    "SELECT * FROM products ORDER BY price DESC",
    "SELECT name, price FROM products ORDER BY 2 DESC, 1",
    "SELECT name AS n FROM customers ORDER BY n",
    "SELECT name, city FROM customers ORDER BY city NULLS FIRST, name",
    "SELECT name, city FROM customers ORDER BY city DESC NULLS LAST",
    "SELECT * FROM products ORDER BY ALL",
    "SELECT name FROM products ORDER BY price",  # order by a non-selected column
    "SELECT * FROM orders ORDER BY order_date LIMIT 3",
    "SELECT * FROM orders ORDER BY id LIMIT 3 OFFSET 4",
    "SELECT * FROM orders ORDER BY id OFFSET 8",
    "SELECT * FROM orders ORDER BY id LIMIT 0",
    "SELECT * FROM orders ORDER BY id LIMIT 30 PERCENT",
    "SELECT DISTINCT city FROM customers ORDER BY city",
    "SELECT DISTINCT status FROM orders",
    "SELECT DISTINCT category, price > 100 AS pricey FROM products ORDER BY ALL",
    "SELECT DISTINCT ON (customer_id) customer_id, id, order_date FROM orders ORDER BY customer_id, order_date DESC",
    "SELECT COUNT(DISTINCT city) FROM customers",
    # --- JOINs -------------------------------------------------------------------
    "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id",
    "SELECT o.id, c.name FROM orders o INNER JOIN customers c ON o.customer_id = c.id ORDER BY o.id",
    "SELECT c.name, o.id FROM customers c LEFT JOIN orders o ON o.customer_id = c.id",
    "SELECT c.name, o.id FROM customers c RIGHT JOIN orders o ON o.customer_id = c.id",
    "SELECT c.name, o.id FROM customers c FULL OUTER JOIN orders o ON o.customer_id = c.id",
    "SELECT c.name, p.name FROM customers c CROSS JOIN products p WHERE p.price > 400",
    "SELECT c.name, p.name FROM customers c, products p WHERE c.id = p.id",
    "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id",
    "SELECT * FROM order_items JOIN orders ON order_items.order_id = orders.id AND orders.status = 'shipped'",
    "SELECT * FROM customers c SEMI JOIN orders o ON o.customer_id = c.id",
    "SELECT * FROM customers c ANTI JOIN orders o ON o.customer_id = c.id",
    "SELECT e.name, m.name AS manager FROM employees e LEFT JOIN employees m ON e.manager_id = m.id",
    "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id JOIN order_items oi ON oi.order_id = o.id "
    "JOIN products p ON p.id = oi.product_id WHERE p.category = 'Electronics' ORDER BY o.id, p.id",
    "SELECT * FROM departments d JOIN employees e USING (id)",
    "SELECT * FROM (SELECT id, name AS n1 FROM customers) a NATURAL JOIN (SELECT id, name AS n2 FROM products) b",
    "SELECT * FROM customers POSITIONAL JOIN products",
    "SELECT c.name, t.total FROM customers c JOIN (SELECT customer_id, COUNT(*) AS total FROM orders GROUP BY customer_id) t "
    "ON t.customer_id = c.id ORDER BY c.name",
    "SELECT * FROM (SELECT 1 AS a, 2 AS b) AS s(x, y)",
    "SELECT v.* FROM (VALUES (1, 'a'), (2, 'b')) AS v(id, label)",
    "SELECT * FROM customers c JOIN (VALUES (1), (3)) AS v(id) ON v.id = c.id",
    "SELECT * FROM range(5) t(n) WHERE n % 2 = 0",
    "SELECT * FROM generate_series(1, 3) g(x) CROSS JOIN (SELECT 'a' AS s) q",
    "SELECT o.id, x.q FROM orders o, LATERAL (SELECT SUM(quantity) AS q FROM order_items oi WHERE oi.order_id = o.id) x ORDER BY o.id",
    "SELECT u.v FROM (SELECT [1, 2, 3] AS l) t, unnest(t.l) AS u(v)",
    "SELECT unnest([1, 2, 3]) AS x",
    "SELECT t.a, b FROM (SELECT 1 AS a) t JOIN (SELECT 1 AS a, 2 AS b) s USING (a)",
    "SELECT * FROM customers c LEFT JOIN orders o ON o.customer_id = c.id AND o.status = 'shipped' WHERE o.id IS NULL",
    # --- GROUP BY / aggregates / HAVING -------------------------------------------------
    "SELECT COUNT(*) FROM orders",
    "SELECT COUNT(*), SUM(price), AVG(price), MIN(price), MAX(price) FROM products",
    "SELECT COUNT(*) FROM orders WHERE status = 'nope'",
    "SELECT COUNT(city), COUNT(*) FROM customers",
    "SELECT status, COUNT(*) FROM orders GROUP BY status",
    "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC, status",
    "SELECT status, COUNT(*) FROM orders GROUP BY 1 ORDER BY 1",
    "SELECT status AS st, COUNT(*) FROM orders GROUP BY st ORDER BY st",
    "SELECT status, customer_id, COUNT(*) FROM orders GROUP BY ALL ORDER BY ALL",
    "SELECT city, COUNT(*) FROM customers GROUP BY city ORDER BY city",  # NULL forms its own group
    "SELECT year(order_date) AS y, month(order_date) AS m, COUNT(*) FROM orders GROUP BY y, m ORDER BY y, m",
    "SELECT category, SUM(price) FILTER (WHERE price > 100) AS big, COUNT(*) FROM products GROUP BY category ORDER BY 1",
    "SELECT category, string_agg(name, ', ' ORDER BY name) AS names FROM products GROUP BY category ORDER BY 1",
    "SELECT category, list(name ORDER BY price) AS l, arg_max(name, price) AS priciest FROM products GROUP BY category ORDER BY 1",
    "SELECT dept_id, median(salary), quantile_cont(salary, 0.9) FROM employees GROUP BY dept_id ORDER BY dept_id",
    "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id HAVING COUNT(*) >= 2 ORDER BY customer_id",
    "SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id HAVING n > 1 ORDER BY customer_id",
    "SELECT customer_id FROM orders GROUP BY customer_id HAVING MAX(order_date) > DATE '2024-09-01' ORDER BY 1",
    "SELECT COUNT(*) FROM orders HAVING COUNT(*) > 5",
    "SELECT SUM(price) / COUNT(*) AS avg_price, COUNT(*) FILTER (WHERE category = 'Electronics') AS n_e FROM products",
    "SELECT c.city, SUM(oi.quantity * p.price) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id "
    "JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id "
    "WHERE o.status <> 'cancelled' GROUP BY c.city HAVING SUM(oi.quantity * p.price) > 500 ORDER BY revenue DESC",
    "SELECT category, price > 100 AS pricey, COUNT(*) FROM products GROUP BY category, price > 100 ORDER BY 1, 2",
    "SELECT d.name, COUNT(e.id) FROM departments d LEFT JOIN employees e ON e.dept_id = d.id GROUP BY d.name ORDER BY d.name",
    "SELECT status, COUNT(*) FROM orders GROUP BY ROLLUP (status) ORDER BY status NULLS LAST",
    "SELECT status, customer_id, COUNT(*) FROM orders GROUP BY CUBE (status, customer_id) ORDER BY ALL",
    "SELECT status, customer_id, COUNT(*) FROM orders GROUP BY GROUPING SETS ((status), (customer_id), ()) ORDER BY ALL",
    "SELECT status, GROUPING(status) AS g, COUNT(*) FROM orders GROUP BY ROLLUP (status) ORDER BY ALL",
    # --- window functions ----------------------------------------------------------
    "SELECT name, salary, ROW_NUMBER() OVER (ORDER BY salary DESC, id) AS rn FROM employees ORDER BY rn",
    "SELECT name, dept_id, RANK() OVER (PARTITION BY dept_id ORDER BY salary DESC) AS r FROM employees ORDER BY dept_id, r, name",
    "SELECT name, DENSE_RANK() OVER (ORDER BY salary DESC) AS r, NTILE(3) OVER (ORDER BY id) AS t FROM employees ORDER BY id",
    "SELECT id, order_date, LAG(order_date) OVER (ORDER BY order_date) AS prev, LEAD(id, 2) OVER (ORDER BY id) FROM orders ORDER BY id",
    "SELECT id, SUM(salary) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running FROM employees ORDER BY id",
    "SELECT id, AVG(salary) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) AS m FROM employees ORDER BY id",
    "SELECT id, FIRST_VALUE(name) OVER w AS f, LAST_VALUE(name) OVER w AS l FROM employees "
    "WINDOW w AS (PARTITION BY dept_id ORDER BY salary ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) ORDER BY id",
    "SELECT name, salary, salary - AVG(salary) OVER (PARTITION BY dept_id) AS diff FROM employees ORDER BY id",
    "SELECT dept_id, SUM(salary) AS total, RANK() OVER (ORDER BY SUM(salary) DESC) AS r FROM employees GROUP BY dept_id ORDER BY r, dept_id",
    "SELECT dept_id, COUNT(*) AS n, SUM(COUNT(*)) OVER () AS all_n FROM employees GROUP BY dept_id ORDER BY dept_id",
    "SELECT customer_id, id FROM orders QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC) = 1 ORDER BY customer_id",
    "SELECT customer_id, id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY id) AS rn FROM orders QUALIFY rn <= 2 ORDER BY 1, 2",
    "SELECT id, COUNT(*) OVER () AS n FROM orders WHERE status = 'shipped' ORDER BY id",
    "SELECT id, SUM(salary) OVER (ORDER BY hire_date RANGE BETWEEN INTERVAL 365 DAY PRECEDING AND CURRENT ROW) FROM employees ORDER BY id",
    # --- subqueries ------------------------------------------------------------------
    "SELECT name FROM products WHERE price > (SELECT AVG(price) FROM products) ORDER BY name",
    "SELECT name FROM customers WHERE id IN (SELECT customer_id FROM orders WHERE status = 'pending') ORDER BY name",
    "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders) ORDER BY name",
    "SELECT name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = 'cancelled')",
    "SELECT name FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)",
    "SELECT name, (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS n FROM customers c ORDER BY name",
    "SELECT name, price, (SELECT MAX(price) FROM products) - price AS gap FROM products ORDER BY gap",
    "SELECT name FROM products WHERE price >= ALL (SELECT price FROM products WHERE category = 'Furniture')",
    "SELECT name FROM products WHERE price < ANY (SELECT price FROM products WHERE category = 'Furniture') ORDER BY 1",
    "SELECT * FROM (SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id) t WHERE n > 1 ORDER BY customer_id",
    "SELECT * FROM (SELECT * FROM (SELECT id, name FROM customers WHERE id > 1) a WHERE id < 5) b ORDER BY id",
    "SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM orders GROUP BY customer_id)",
    # --- CTEs ------------------------------------------------------------------------
    "WITH t AS (SELECT * FROM orders WHERE status = 'shipped') SELECT customer_id, COUNT(*) FROM t GROUP BY 1 ORDER BY 1",
    "WITH a AS (SELECT id, price FROM products), b AS (SELECT id, price * 2 AS p2 FROM a) SELECT * FROM a JOIN b USING (id) ORDER BY id",
    "WITH t AS (SELECT 1 AS x UNION ALL SELECT 2) SELECT t1.x, t2.x FROM t t1 CROSS JOIN t t2 ORDER BY ALL",
    "WITH t AS MATERIALIZED (SELECT * FROM customers) SELECT name FROM t WHERE id = 2",
    "WITH t(a, b) AS (SELECT id, name FROM customers) SELECT a, b FROM t WHERE a > 4",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT * FROM r",
    "WITH RECURSIVE chain AS (SELECT id, name, manager_id, 1 AS level FROM employees WHERE manager_id IS NULL "
    "UNION ALL SELECT e.id, e.name, e.manager_id, c.level + 1 FROM employees e JOIN chain c ON e.manager_id = c.id) "
    "SELECT * FROM chain ORDER BY level, id",
    "WITH RECURSIVE r AS (SELECT 1 AS n UNION SELECT (n % 3) + 1 FROM r) SELECT * FROM r ORDER BY n",
    "WITH RECURSIVE fib(a, b) AS (SELECT 0, 1 UNION ALL SELECT b, a + b FROM fib WHERE b < 50) SELECT a FROM fib",
    "SELECT * FROM (WITH x AS (SELECT 1 AS v) SELECT v FROM x) s",
    # --- set operations ---------------------------------------------------------------------
    "SELECT city FROM customers UNION SELECT 'Tainan' ORDER BY city",
    "SELECT city FROM customers UNION ALL SELECT city FROM customers ORDER BY city",
    "SELECT customer_id FROM orders INTERSECT SELECT id FROM customers ORDER BY 1",
    "SELECT id FROM customers EXCEPT SELECT customer_id FROM orders ORDER BY 1",
    "SELECT customer_id FROM orders INTERSECT ALL SELECT customer_id FROM orders WHERE status = 'shipped' ORDER BY 1",
    "SELECT customer_id FROM orders EXCEPT ALL SELECT id FROM customers ORDER BY 1",
    "SELECT 1 AS a, 2 AS b UNION BY NAME SELECT 3 AS b, 4 AS a ORDER BY a",
    "(SELECT id FROM customers ORDER BY id LIMIT 2) UNION ALL (SELECT id FROM products ORDER BY id DESC LIMIT 2)",
    "SELECT 1 AS x UNION SELECT 2 UNION SELECT 1 ORDER BY x",
    "SELECT id FROM customers UNION ALL SELECT id FROM products ORDER BY id LIMIT 4 OFFSET 2",
    "SELECT status, COUNT(*) FROM orders GROUP BY status UNION ALL SELECT 'total', COUNT(*) FROM orders ORDER BY 2",
    # --- misc DuckDB syntax -------------------------------------------------------------
    "SELECT * FROM (VALUES (1, 'x'), (2, NULL)) t(a, b) WHERE b IS NULL",
    "SELECT {'k': id} AS s, [id, id * 2] AS l FROM customers ORDER BY id",
    "SELECT name, name[1:2] AS pre FROM customers ORDER BY id",
    "SELECT id, order_date, customer_id FROM orders WHERE customer_id = 1 ORDER BY 2 DESC",
    "SELECT \"name\" AS \"Name With Space\", id AS 編號 FROM customers ORDER BY 編號",
    "SELECT 1 AS a, 1 AS a",
    "SELECT a.id, b.id FROM customers a JOIN customers b ON a.id = b.id - 1 ORDER BY a.id",
    "SELECT * FROM customers WHERE id = 3 AND name = 'Cathy' OR city = 'Kaohsiung'",
    "SELECT greatest(1, 5, 3), least('b', 'a'), 7 // 2, 7 % 3, 2 ** 10",
    "SELECT hash(1) IS NOT NULL",
    "SELECT * FROM order_items ORDER BY quantity DESC, order_id, product_id LIMIT 5",
    "SELECT * FROM (SELECT * FROM products ORDER BY price DESC LIMIT 3) ORDER BY name",
]

# Correct final results are required; step-by-step decomposition may fall back.
LOOSE = [
    "SELECT random() < 2 AS always",
    "SELECT * FROM customers USING SAMPLE 100%",
    "PIVOT orders ON status USING COUNT(*) GROUP BY customer_id ORDER BY customer_id",
    "UNPIVOT (SELECT 1 AS a, 2 AS b) ON a, b INTO NAME k VALUE v",
    "DESCRIBE customers",
    "SHOW TABLES",
    "SUMMARIZE products",
    "EXPLAIN SELECT * FROM customers",
    "SELECT * FROM duckdb_functions() LIMIT 1",
    "SELECT current_date IS NOT NULL",
    "SELECT * FROM customers WHERE id IN (SELECT id FROM customers WHERE random() < 2) ORDER BY id",
    "SELECT * FROM (SELECT 1 AS t, 10 AS v UNION ALL SELECT 5, 50) a ASOF JOIN (SELECT 3 AS t, 'x' AS tag) b ON a.t >= b.t",
]


def _ids(queries):
    return [q[:70] for q in queries]


@pytest.mark.parametrize("sql", STRICT, ids=_ids(STRICT))
def test_strict(tracer, session, sql):
    result = tracer.run(sql)
    assert result["error"] is None, result["error"]
    main = main_pipeline(result)
    final = main["steps"][-1]
    assert final["kind"] == "result"
    cols, rows = direct(session, sql)
    assert final["panels"][-1]["columns"] == cols
    assert final["panels"][-1]["rows"] == rows
    for st in result["statements"]:
        for p in st["pipelines"]:
            assert not any("無法逐步拆解" in n for n in p["notes"]), (p["title"], p["notes"])
            for s in p["steps"]:
                assert not any("不一致" in n for n in s.get("notes", [])), (p["title"], s["notes"])
    assert len(main["steps"]) > 1 or sql.startswith("SELECT 42")
    check_invariants(result)


@pytest.mark.parametrize("sql", LOOSE, ids=_ids(LOOSE))
def test_loose(tracer, session, sql):
    result = tracer.run(sql)
    assert result["error"] is None, result["error"]
    final = main_pipeline(result)["steps"][-1]
    cols, rows = direct(session, sql)
    assert final["panels"][-1]["columns"] == cols
    if "random" not in sql and "current_date" not in sql and "SAMPLE" not in sql and "EXPLAIN" not in sql:
        assert final["panels"][-1]["rows"] == rows
    check_invariants(result)


def test_examples_all_trace(tracer):
    from sqlstepper.samples import EXAMPLES

    for ex in EXAMPLES:
        result = tracer.run(ex["sql"])
        assert result["error"] is None, (ex["title"], result["error"])
        check_invariants(result)

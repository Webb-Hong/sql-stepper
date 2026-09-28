"""Built-in sample tables and example queries.

The data is deliberately small and includes the awkward cases that make SQL
semantics visible: NULLs, an order whose customer does not exist, a customer
without orders, duplicate values, and a manager hierarchy for recursive CTEs.
"""

SAMPLE_SQL = """
CREATE OR REPLACE TABLE customers (id INTEGER, name VARCHAR, city VARCHAR, signup_date DATE);
INSERT INTO customers VALUES
  (1, 'Alice', 'Taipei',    DATE '2024-01-05'),
  (2, 'Bob',   'Taichung',  DATE '2024-02-11'),
  (3, 'Cathy', 'Taipei',    DATE '2024-03-20'),
  (4, 'David', NULL,        DATE '2024-04-02'),
  (5, 'Eva',   'Kaohsiung', DATE '2024-05-17'),
  (6, 'Frank', 'Taichung',  DATE '2024-06-30');

CREATE OR REPLACE TABLE products (id INTEGER, name VARCHAR, category VARCHAR, price DECIMAL(10, 2));
INSERT INTO products VALUES
  (1, 'Laptop',   'Electronics', 1200.00),
  (2, 'Mouse',    'Electronics',   25.00),
  (3, 'Monitor',  'Electronics',  300.00),
  (4, 'Desk',     'Furniture',    450.00),
  (5, 'Chair',    'Furniture',    150.00),
  (6, 'Notebook', 'Stationery',     3.50);

CREATE OR REPLACE TABLE orders (id INTEGER, customer_id INTEGER, order_date DATE, status VARCHAR);
INSERT INTO orders VALUES
  (101, 1, DATE '2024-07-01', 'shipped'),
  (102, 2, DATE '2024-07-03', 'shipped'),
  (103, 1, DATE '2024-07-08', 'cancelled'),
  (104, 3, DATE '2024-07-15', 'shipped'),
  (105, 5, DATE '2024-08-02', 'pending'),
  (106, 2, DATE '2024-08-09', 'shipped'),
  (107, 9, DATE '2024-08-20', 'shipped'),
  (108, 3, DATE '2024-09-01', 'pending'),
  (109, 1, DATE '2024-09-12', 'shipped'),
  (110, 6, DATE '2024-09-25', 'cancelled');

CREATE OR REPLACE TABLE order_items (order_id INTEGER, product_id INTEGER, quantity INTEGER);
INSERT INTO order_items VALUES
  (101, 1, 1), (101, 2, 2), (102, 2, 1), (103, 3, 1), (104, 4, 1),
  (104, 5, 2), (105, 6, 10), (106, 1, 1), (106, 3, 2), (107, 2, 3),
  (108, 5, 4), (109, 3, 1), (109, 6, 5), (110, 4, 1);

CREATE OR REPLACE TABLE departments (id INTEGER, name VARCHAR);
INSERT INTO departments VALUES (1, 'Engineering'), (2, 'Sales'), (3, 'Marketing'), (4, 'Legal');

CREATE OR REPLACE TABLE employees (
  id INTEGER, name VARCHAR, dept_id INTEGER, manager_id INTEGER, salary INTEGER, hire_date DATE);
INSERT INTO employees VALUES
  (1,  'Grace',  1, NULL, 150000, DATE '2018-03-01'),
  (2,  'Linus',  1, 1,    120000, DATE '2019-06-15'),
  (3,  'Ada',    1, 2,     98000, DATE '2021-01-10'),
  (4,  'Ken',    1, 2,     98000, DATE '2022-08-22'),
  (5,  'Susan',  2, 1,    110000, DATE '2019-11-03'),
  (6,  'Tom',    2, 5,     72000, DATE '2021-05-18'),
  (7,  'Mia',    2, 5,     65000, DATE '2023-02-27'),
  (8,  'Omar',   3, 1,     90000, DATE '2020-09-09'),
  (9,  'Nina',   3, 8,     61000, DATE '2023-07-01'),
  (10, 'Paul',   NULL, 8,  58000, DATE '2024-01-15');
"""

EXAMPLES = [
    {
        "title": "JOIN：訂單加上顧客與商品",
        "sql": """SELECT o.id AS order_id,
       c.name AS customer,
       p.name AS product,
       oi.quantity
FROM orders o
JOIN customers c ON o.customer_id = c.id
JOIN order_items oi ON oi.order_id = o.id
JOIN products p ON p.id = oi.product_id
WHERE o.status = 'shipped'
ORDER BY o.id
LIMIT 8;""",
    },
    {
        "title": "LEFT JOIN 與 NULL：沒有下單的顧客",
        "sql": """SELECT c.name, o.id AS order_id, o.status
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
ORDER BY c.id, o.id;""",
    },
    {
        "title": "GROUP BY + HAVING：各城市營收",
        "sql": """SELECT c.city,
       COUNT(DISTINCT o.id) AS orders,
       SUM(oi.quantity * p.price) AS revenue
FROM orders o
JOIN customers c ON c.id = o.customer_id
JOIN order_items oi ON oi.order_id = o.id
JOIN products p ON p.id = oi.product_id
WHERE o.status <> 'cancelled'
GROUP BY c.city
HAVING SUM(oi.quantity * p.price) > 500
ORDER BY revenue DESC;""",
    },
    {
        "title": "視窗函數：部門內薪資排名",
        "sql": """SELECT e.name,
       d.name AS dept,
       e.salary,
       RANK() OVER (PARTITION BY e.dept_id ORDER BY e.salary DESC) AS salary_rank,
       e.salary - AVG(e.salary) OVER (PARTITION BY e.dept_id) AS diff_from_avg
FROM employees e
LEFT JOIN departments d ON d.id = e.dept_id
ORDER BY dept, salary_rank;""",
    },
    {
        "title": "QUALIFY：每位顧客最近一筆訂單",
        "sql": """SELECT customer_id, id AS order_id, order_date
FROM orders
QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC) = 1
ORDER BY customer_id;""",
    },
    {
        "title": "CTE：先算每筆訂單金額再彙總",
        "sql": """WITH order_totals AS (
  SELECT oi.order_id, SUM(oi.quantity * p.price) AS total
  FROM order_items oi
  JOIN products p ON p.id = oi.product_id
  GROUP BY oi.order_id
)
SELECT o.customer_id, COUNT(*) AS n_orders, AVG(t.total) AS avg_total
FROM orders o
JOIN order_totals t ON t.order_id = o.id
GROUP BY o.customer_id
ORDER BY avg_total DESC;""",
    },
    {
        "title": "遞迴 CTE：組織階層",
        "sql": """WITH RECURSIVE chain AS (
  SELECT id, name, manager_id, 1 AS level
  FROM employees
  WHERE manager_id IS NULL
  UNION ALL
  SELECT e.id, e.name, e.manager_id, c.level + 1
  FROM employees e
  JOIN chain c ON e.manager_id = c.id
)
SELECT * FROM chain ORDER BY level, id;""",
    },
    {
        "title": "子查詢：高於平均價格的商品",
        "sql": """SELECT name, category, price
FROM products
WHERE price > (SELECT AVG(price) FROM products)
ORDER BY price DESC;""",
    },
    {
        "title": "UNION / EXCEPT：集合運算",
        "sql": """SELECT city FROM customers WHERE city IS NOT NULL
EXCEPT
SELECT c.city FROM customers c JOIN orders o ON o.customer_id = c.id
WHERE o.status = 'cancelled'
ORDER BY city;""",
    },
    {
        "title": "DISTINCT 與 ORDER BY / LIMIT",
        "sql": """SELECT DISTINCT p.category, oi.quantity
FROM order_items oi
JOIN products p ON p.id = oi.product_id
ORDER BY p.category, oi.quantity DESC
LIMIT 5 OFFSET 1;""",
    },
    {
        "title": "DML：UPDATE / DELETE / INSERT",
        "sql": """UPDATE products SET price = price * 1.1 WHERE category = 'Furniture';

DELETE FROM orders WHERE status = 'cancelled';

INSERT INTO customers
SELECT 7, 'Gina', 'Tainan', DATE '2024-10-01';

SELECT * FROM products;""",
    },
]

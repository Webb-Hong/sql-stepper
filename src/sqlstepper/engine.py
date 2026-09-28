"""Decompose SQL statements into a sequence of visualizable steps.

Every step is computed by running a real query on DuckDB, so intermediate
results follow DuckDB semantics exactly. The logical order of a SELECT is:

    FROM -> JOIN... -> WHERE -> GROUP BY -> aggregates -> HAVING
         -> window functions -> QUALIFY -> SELECT list -> DISTINCT
         -> ORDER BY -> LIMIT/OFFSET

Row lineage: each FROM/JOIN source is wrapped as
``(SELECT *, ROW_NUMBER() OVER () AS __rid_<i> FROM source) AS alias`` so every
intermediate row carries the ids of the source rows it came from. Columns whose
names start with ``__`` are bookkeeping and never shown as data.

If a decomposition attempt fails (exotic syntax), it retries with fewer
features, and finally falls back to showing only the statement's result.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType as TT

from .db import Rows, Session, to_json_value

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # unparsable statements are expected; we fall back

DIALECT = "duckdb"
DISPLAY_ROWS = 300  # rows per panel sent to the browser
MAX_DEPTH = 3  # nesting depth for CTE / subquery pipelines
MAX_RECURSION = 60  # iterations shown for a recursive CTE
RID = "__rid_"
QUERY_TYPES = (exp.Select, exp.SetOperation)
_STOPS = {
    TT.SELECT, TT.DISTINCT, TT.FROM, TT.WHERE, TT.GROUP_BY, TT.HAVING, TT.QUALIFY, TT.WINDOW, TT.ORDER_BY,
    TT.LIMIT, TT.OFFSET, TT.FETCH, TT.UNION, TT.INTERSECT, TT.EXCEPT, TT.SEMICOLON, TT.WITH,
    TT.JOIN, TT.LEFT, TT.RIGHT, TT.FULL, TT.INNER, TT.OUTER, TT.CROSS, TT.NATURAL, TT.SEMI, TT.ANTI,
    TT.ASOF, TT.POSITIONAL, TT.LATERAL, TT.SET, TT.RETURNING,
}


class Unsupported(Exception):
    """This query shape cannot be decomposed with the current options."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def gen(node: exp.Expression, pretty: bool = False) -> str:
    return node.sql(dialect=DIALECT, pretty=pretty)


def short(node, n: int = 80) -> str:
    text = node if isinstance(node, str) else gen(node)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def alias(node: exp.Expression, name: str) -> exp.Alias:
    return exp.alias_(node, name, quoted=True)


def rid_key(values) -> str:
    return "|".join("∅" if v is None else str(v) for v in values)


def row_sig(row) -> str:
    return repr([to_json_value(v) for v in row])


def with_ctx(q: exp.Expression, ctx: exp.With | None) -> exp.Expression:
    q = q.copy()
    if ctx is not None and ctx.expressions:
        q.set("with_", ctx.copy())
    return q


def scope_walk(node: exp.Expression | None):
    """Pre-order walk that does not enter nested queries."""
    if node is None:
        return
    yield node
    for child in node.iter_expressions():
        if isinstance(child, (exp.Subquery, *QUERY_TYPES)):
            continue
        yield from scope_walk(child)


def _is_window_body(node: exp.Expression) -> bool:
    parent = node.parent
    while isinstance(parent, (exp.Filter, exp.WithinGroup, exp.RespectNulls, exp.IgnoreNulls)):
        node, parent = parent, parent.parent
    return isinstance(parent, exp.Window) and parent.this is node


def collect_aggregates(nodes) -> list[exp.Expression]:
    found, seen = [], set()

    def visit(n):
        if n is None:
            return
        if isinstance(n, exp.AggFunc) and not _is_window_body(n):
            top = n
            while isinstance(top.parent, (exp.Filter, exp.WithinGroup)):
                top = top.parent
            key = gen(top)
            if key not in seen:
                seen.add(key)
                found.append(top)
            return
        if isinstance(n, exp.Window):
            for part in ("partition_by", "order"):  # aggregates may sit inside OVER (...)
                v = n.args.get(part)
                for c in v if isinstance(v, list) else [v]:
                    visit(c)
            return
        for c in n.iter_expressions():
            if not isinstance(c, (exp.Subquery, *QUERY_TYPES)):
                visit(c)

    for node in nodes:
        visit(node)
    return found


def collect_windows(nodes) -> list[exp.Window]:
    found, seen = [], set()
    for node in nodes:
        for n in scope_walk(node):
            if isinstance(n, exp.Window) and gen(n) not in seen:
                seen.add(gen(n))
                found.append(n)
    return found


def value_links(out: Rows, inputs: list[tuple[Rows, list[str]]], consume: bool = True) -> list[list]:
    """Link output rows to identical rows in the input panels (first unused match)."""
    index = []
    for rows, ids in inputs:
        m: dict[str, list[str]] = {}
        for r, i in zip(rows.rows, ids):
            m.setdefault(row_sig(r), []).append(i)
        index.append(m)
    links = []
    for r in out.rows[:DISPLAY_ROWS]:
        sig, link = row_sig(r), []
        for p, m in enumerate(index):
            cands = m.get(sig)
            if cands:
                link.append([p, cands.pop(0) if consume else cands[0]])
                if consume:
                    break
        links.append(link)
    return links


def panel(title: str, rows: Rows, *, ids=None, marks=None, groups=None, highlight=(), role="output",
          status=None, changed=None) -> dict:
    n = min(len(rows.rows), DISPLAY_ROWS)
    p = {
        "title": title,
        "role": role,
        "columns": list(rows.columns),
        "rows": [[to_json_value(v) for v in r] for r in rows.rows[:n]],
        "total": len(rows.rows),
        "overflow": rows.overflow,
    }
    if ids is not None:
        p["ids"] = [None if i is None else str(i) for i in ids[:n]]
    if marks is not None:
        p["marks"] = [None if m is None else bool(m) for m in marks[:n]]
    if groups is not None:
        p["groups"] = [None if g is None else int(g) for g in groups[:n]]
    if highlight:
        p["highlight"] = list(highlight)
    if status is not None:
        p["status"] = status[:n]
    if changed is not None:
        p["changed"] = changed[:n]
    return p


@dataclass
class Current:
    """The row set flowing between steps."""

    rows: Rows
    ids: list[str]
    title: str

    def subset(self, flags) -> "Current":
        flags = [f is True for f in flags]
        return Current(self.rows.subset(flags), [i for i, f in zip(self.ids, flags) if f], self.title)

    def as_input(self, title=None, **kw) -> dict:
        return panel(title or self.title, self.rows, ids=self.ids, role="input", **kw)


JOIN_EXPLAIN = {
    "INNER": "INNER JOIN：左邊每一列去右邊找符合 ON 條件的列，找到幾列就產生幾列；找不到的列會被丟掉。",
    "LEFT": "LEFT JOIN：跟 INNER JOIN 一樣配對，但左邊找不到對象的列仍然保留，右邊的欄位補 NULL。",
    "RIGHT": "RIGHT JOIN：右邊找不到對象的列仍然保留，左邊的欄位補 NULL。",
    "FULL": "FULL JOIN：兩邊找不到對象的列都保留，缺的那一側補 NULL。",
    "CROSS": "CROSS JOIN：左邊每一列都跟右邊每一列配對（笛卡兒積），列數 = 左 × 右。",
    "COMMA": "逗號 FROM a, b 等於 CROSS JOIN：先產生所有組合，配對條件通常寫在 WHERE。",
    "SEMI": "SEMI JOIN：只保留在右邊找得到對象的左邊列，而且不會因為多筆配對而重複；結果只有左邊的欄位。",
    "ANTI": "ANTI JOIN：只保留在右邊找不到對象的左邊列（等同 NOT EXISTS）。",
    "NATURAL": "NATURAL JOIN：自動用兩邊同名的欄位當配對條件。",
    "ASOF": "ASOF JOIN：依不等式條件，替每一列找「最接近且不超過」的那一列，常用於時間序列。",
    "POSITIONAL": "POSITIONAL JOIN：依列的位置一對一並排（第 1 列配第 1 列）。",
    "LATERAL": "LATERAL：右邊的子查詢會針對左邊每一列各算一次，可以引用左邊的欄位。",
}


def join_type(j: exp.Join) -> str:
    kind, side, method = (j.kind or "").upper(), (j.side or "").upper(), (j.method or "").upper()
    if isinstance(j.this, exp.Lateral):
        return "LATERAL"
    if method in ("NATURAL", "ASOF", "POSITIONAL"):
        return method
    if kind in ("SEMI", "ANTI", "CROSS"):
        return kind
    if side in ("LEFT", "RIGHT", "FULL"):
        return side
    if not kind and not j.args.get("on") and not j.args.get("using"):
        return "COMMA"
    return "INNER"


# ---------------------------------------------------------------------------
# statement tracer
# ---------------------------------------------------------------------------
class Tracer:
    def __init__(self, session: Session):
        self.s = session

    def run(self, script: str, sandbox: bool = True) -> dict:
        with self.s.lock:
            try:
                stmts = split_script(script)
            except duckdb.Error as e:
                return {"statements": [], "error": str(e), "sandbox": sandbox}
            out = {"statements": [], "error": None, "sandbox": sandbox}
            box = self.s.sandbox() if sandbox else _null()
            with box, self.s.deterministic():
                for n, (off, text) in enumerate(stmts):
                    res = StatementTracer(self.s, text, script.count("\n", 0, off), n).trace()
                    out["statements"].append(res)
                    if res["error"]:
                        out["error"] = res["error"]
                        break
            return out


def split_script(script: str) -> list[tuple[int, str]]:
    """Split on top-level semicolons, returning (offset, text) of each statement.

    DuckDB's extract_statements() rewrites some statements (PIVOT expands into
    several internal ones with unusable text), so the user's text is kept instead.
    """
    try:
        toks = sqlglot.Dialect.get_or_raise(DIALECT).tokenize(script)
    except Exception:  # noqa: BLE001 - e.g. unterminated string: let DuckDB report it
        duckdb.connect().extract_statements(script)
        return [(0, script.strip())] if script.strip() else []
    out, group = [], []
    for t in toks + [None]:
        if t is None or t.token_type == TT.SEMICOLON:
            if group:
                out.append((group[0].start, script[group[0].start: group[-1].end + 1]))
            group = []
        else:
            group.append(t)
    return out


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class StatementTracer:
    def __init__(self, session: Session, text: str, line0: int, index: int):
        self.s = session
        self.text = text
        self.line0 = line0
        self.index = index
        self.pipelines: list[dict] = []
        self.tmp_counter = 0

    # ---- plumbing ---------------------------------------------------------
    def exec(self, sql: str | exp.Expression) -> Rows:
        return self.s.query(sql if isinstance(sql, str) else gen(sql))

    def line_of(self, node: exp.Expression | None, keyword: str | None = None) -> int | None:
        if node is None:
            return None
        starts = [n.meta["start"] for n in node.walk() if "start" in n.meta]
        if not starts:
            return None
        pos = min(starts)
        if keyword:
            last = None
            for m in re.finditer(keyword, self.text[:pos], re.I):
                last = m
            if last:
                pos = last.start()
        return self.line0 + self.text.count("\n", 0, pos) + 1

    def src(self, nodes, stop_comma: bool = False, extra_stops=()) -> str:
        """The user's own text for an AST node (or list of nodes), for readable step titles.

        Expands from the positions sqlglot recorded for identifiers/literals out to the
        surrounding clause keywords, keeping parentheses balanced.
        """
        nodes = [n for n in (nodes if isinstance(nodes, list) else [nodes]) if n is not None]
        spans = [(n.meta["start"], n.meta["end"]) for node in nodes for n in node.walk() if "start" in n.meta]
        if not spans:
            return ", ".join(gen(n) for n in nodes)
        if not hasattr(self, "_tokens"):
            try:
                self._tokens = sqlglot.Dialect.get_or_raise(DIALECT).tokenize(self.text)
            except Exception:  # noqa: BLE001
                self._tokens = []
        toks = self._tokens
        a, b = min(s for s, _ in spans), max(e for _, e in spans)
        inside = [i for i, t in enumerate(toks) if t.start >= a and t.end <= b]
        if not inside:
            return ", ".join(gen(n) for n in nodes)
        i0, i1 = inside[0], inside[-1]
        bal, low = 0, 0
        for t in toks[i0:i1 + 1]:
            bal += (t.token_type == TT.L_PAREN) - (t.token_type == TT.R_PAREN)
            low = min(low, bal)
        stops = _STOPS | set(extra_stops) | ({TT.COMMA} if stop_comma else set())
        depth = -low  # unmatched ')' inside the span: walk back to their '('
        while i0 > 0:
            t = toks[i0 - 1]
            if t.token_type == TT.L_PAREN:
                if depth == 0:
                    break
                depth -= 1
            elif t.token_type == TT.R_PAREN:
                depth += 1
            elif depth == 0 and t.token_type in stops:
                break
            i0 -= 1
        depth = bal - low  # unmatched '(' inside the span: walk forward to their ')'
        while i1 + 1 < len(toks):
            t = toks[i1 + 1]
            if t.token_type == TT.R_PAREN:
                if depth == 0:
                    break
                depth -= 1
            elif t.token_type == TT.L_PAREN:
                depth += 1
            elif depth == 0 and t.token_type in stops:
                break
            i1 += 1
        return re.sub(r"\s+", " ", self.text[toks[i0].start: toks[i1].end + 1]).strip()

    def add_pipeline(self, title: str, depth: int, steps: list[dict], notes=()) -> dict:
        pid = f"q{self.index}p{len(self.pipelines)}"
        for i, st in enumerate(steps):
            st["id"] = f"{pid}s{i}"
            st.setdefault("notes", [])
        p = {"id": pid, "title": title, "depth": depth, "steps": steps, "notes": list(notes)}
        self.pipelines.append(p)
        return p

    # ---- entry ------------------------------------------------------------
    def trace(self) -> dict:
        res = {"sql": self.text, "line": self.line0 + 1, "pipelines": self.pipelines, "error": None}
        try:
            ast = sqlglot.parse_one(self.text, read=DIALECT)
        except Exception:
            ast = None
        try:
            if isinstance(ast, (exp.Subquery, *QUERY_TYPES)) and _returns_rows(self.s, self.text):
                final = self.exec(self.text)  # user errors surface here, before any decomposition
                self.safe_decompose(ast, None, "主查詢", 0, final=final, final_sql=self.text)
            elif isinstance(ast, (exp.Insert, exp.Update, exp.Delete)):
                self.trace_dml(ast)
            elif isinstance(ast, exp.Create) and isinstance(ast.expression, (exp.Subquery, *QUERY_TYPES)):
                self.trace_create(ast)
            else:
                self.trace_generic()
        except duckdb.Error as e:
            res["error"] = str(e)
        return res

    def safe_decompose(self, q, ctx, title, depth, final=None, final_sql=None):
        mark = len(self.pipelines)
        try:
            return self.decompose(q, ctx, title, depth, final=final, final_sql=final_sql)
        except Exception as e:  # noqa: BLE001 - any failure falls back to showing the result
            del self.pipelines[mark:]
            note = f"這個查詢無法逐步拆解，只顯示結果（{type(e).__name__}: {short(str(e), 160)}）"
            try:
                return self.single(q, ctx, title, depth, final=final, final_sql=final_sql, notes=[note])
            except Exception:  # noqa: BLE001
                del self.pipelines[mark:]
                return None, None

    # ---- query decomposition ----------------------------------------------
    def decompose(self, q, ctx, title, depth, final=None, final_sql=None):
        """Append the pipeline(s) for query q. Returns (pipeline, final_rows)."""
        while isinstance(q, exp.Subquery) and not q.alias and not _has_modifiers(q):
            q = q.this
        own = q.args.get("with_")
        if own is not None:
            ctx = self.expand_ctes(own, ctx, depth)
            q = q.copy()
            q.set("with_", None)
        if isinstance(q, exp.Select):
            return SelectPipeline(self, q, ctx, title, depth, final, final_sql).build()
        if isinstance(q, exp.SetOperation):
            return self.setop(q, ctx, title, depth, final, final_sql)
        return self.single(q, ctx, title, depth, final=final, final_sql=final_sql)

    def single(self, q, ctx, title, depth, final=None, final_sql=None, notes=()):
        sql = final_sql or gen(with_ctx(q, ctx))
        rows = final if final is not None else self.exec(sql)
        step = {
            "kind": "result", "clause": "RESULT", "title": "查詢結果",
            "explain": "直接執行這段查詢得到的結果。",
            "sql": sql, "line": self.line_of(q),
            "panels": [panel("結果", rows)], "anim": "reveal", "links": [],
        }
        return self.add_pipeline(title, depth, [step], notes), rows

    def expand_ctes(self, own: exp.With, ctx: exp.With | None, depth: int) -> exp.With:
        exprs = [e.copy() for e in ctx.expressions] if ctx is not None else []
        recursive = bool(own.args.get("recursive") or (ctx is not None and ctx.args.get("recursive")))
        for cte in own.expressions:
            cur = exp.With(expressions=[e.copy() for e in exprs], recursive=recursive) if exprs else None
            name = cte.alias
            if depth < MAX_DEPTH:
                body = cte.this
                self_ref = any(t.name == name and not t.db for t in body.find_all(exp.Table))
                if recursive and self_ref and isinstance(body, exp.Union):
                    mark = len(self.pipelines)
                    try:
                        self.recursive_cte(cte, cur, depth + 1)
                    except Exception:  # noqa: BLE001
                        del self.pipelines[mark:]
                        rec_ctx = exp.With(expressions=[e.copy() for e in exprs] + [cte.copy()], recursive=True)
                        self.single(exp.select("*").from_(exp.to_table(name)), rec_ctx, f"遞迴 CTE {name}", depth + 1)
                elif not self_ref:
                    self.safe_decompose(body, cur, f"CTE {name}", depth + 1)
            exprs.append(cte.copy())
        return exp.With(expressions=exprs, recursive=recursive)

    def recursive_cte(self, cte: exp.CTE, ctx: exp.With | None, depth: int):
        name, body = cte.alias, cte.this
        anchor, rec = body.this, body.expression
        union_all = not body.args.get("distinct")
        col_alias = [c.name for c in (cte.args["alias"].columns or [])]
        self.tmp_counter += 1
        prefix = f"__rec{self.index}_{self.tmp_counter}"
        acc = f"{prefix}_acc"
        created = []

        def as_named(q: exp.Expression, cols: list[str]) -> str:
            inner = gen(with_ctx(q, ctx))
            if cols:
                return f"SELECT * FROM ({inner}) AS __r({', '.join(_qi(c) for c in cols)})"
            return inner

        steps = []
        try:
            first = f"{prefix}_0"
            self.s.con.execute(f"CREATE TEMP TABLE {first} AS {as_named(anchor, col_alias)}")
            created.append(first)
            rows = self.exec(f"SELECT * FROM {first}")
            cols = rows.columns
            dedupe = "" if union_all else "DISTINCT "
            self.s.con.execute(f"CREATE TEMP TABLE {acc} AS SELECT {dedupe}* FROM {first}")
            created.append(acc)
            steps.append({
                "kind": "source", "clause": "RECURSIVE", "title": "第 0 輪：起始查詢（anchor）",
                "explain": f"遞迴 CTE 先執行 UNION 上方的起始查詢，得到第一批列。接著每一輪都拿「上一輪新產生的列」代入 {name}，重複到沒有新列為止。",
                "sql": as_named(anchor, col_alias), "line": self.line_of(anchor),
                "panels": [panel("第 0 輪產生的列", rows)], "anim": "reveal", "links": [],
            })
            prev, prev_rows = first, rows
            for k in range(1, MAX_RECURSION + 1):
                def swap(n, prev=prev):
                    if isinstance(n, exp.Table) and n.name == name and not n.db:
                        return exp.Table(this=exp.to_identifier(prev),
                                         alias=n.args.get("alias") or exp.TableAlias(this=exp.to_identifier(name)))
                    return n
                rec_k = rec.copy().transform(swap)
                sql = as_named(rec_k, cols)
                if not union_all:
                    sql = f"({sql}) EXCEPT SELECT * FROM {acc}"
                cur = f"{prefix}_{k}"
                self.s.con.execute(f"CREATE TEMP TABLE {cur} AS {sql}")
                created.append(cur)
                rows = self.exec(f"SELECT * FROM {cur}")
                self.s.con.execute(f"INSERT INTO {acc} SELECT * FROM {cur}")
                done = not rows.rows
                steps.append({
                    "kind": "source", "clause": "RECURSIVE",
                    "title": f"第 {k} 輪：代入上一輪的 {len(prev_rows.rows)} 列" + ("（沒有新列，結束）" if done else ""),
                    "explain": f"把 {name} 換成第 {k - 1} 輪的結果，執行 UNION 下方的遞迴查詢。"
                               + ("" if union_all else "UNION（非 ALL）會去掉已經出現過的列。")
                               + ("這一輪沒有產生新列，遞迴停止。" if done else ""),
                    "sql": sql, "line": self.line_of(rec),
                    "panels": [panel(f"第 {k - 1} 輪的列（代入 {name}）", prev_rows, role="input"),
                               panel(f"第 {k} 輪產生的列", rows)],
                    "anim": "reveal", "links": [],
                })
                if done:
                    break
                prev, prev_rows = cur, rows
            else:
                steps[-1]["notes"] = [f"超過 {MAX_RECURSION} 輪，停止展開（實際查詢仍會繼續）"]
            total = self.exec(f"SELECT * FROM {acc}")
            full_ctx = exp.With(expressions=([e.copy() for e in ctx.expressions] if ctx else []) + [cte.copy()],
                                recursive=True)
            final_sql = gen(with_ctx(exp.select("*").from_(exp.to_table(name)), full_ctx))
            final = self.exec(final_sql)
            step = {
                "kind": "result", "clause": "RESULT", "title": f"{name} 的完整內容",
                "explain": "所有輪次產生的列合在一起，就是遞迴 CTE 的內容。",
                "sql": final_sql, "line": self.line_of(cte, r"\bWITH\b"),
                "panels": [panel(name, final)], "anim": "reveal", "links": [], "notes": [],
            }
            if not _same_rows(total, final, ordered=False):
                step["notes"].append("逐輪展開的結果與實際執行不一致，請以此結果為準。")
            steps.append(step)
            self.add_pipeline(f"遞迴 CTE {name}", depth, steps)
        finally:
            for t in created:
                self.s.con.execute(f"DROP TABLE IF EXISTS {t}")

    def setop(self, q: exp.SetOperation, ctx, title, depth, final, final_sql):
        op = type(q).__name__.upper()
        op_text = op + ("" if q.args.get("distinct") else " ALL") + (" BY NAME" if q.args.get("by_name") else "")
        sides = []
        for label, part in (("上方", q.this), ("下方", q.expression)):
            if depth < MAX_DEPTH:
                _, rows = self.safe_decompose(part, ctx, f"{title}・{op_text} {label}", depth + 1)
            else:
                rows = None
            if rows is None:
                rows = self.exec(with_ctx(part, ctx))
            sides.append(Current(rows, [f"{label}{i}" for i in range(len(rows.rows))], f"{label}的查詢結果"))
        core = q.copy()
        for k in ("order", "limit", "offset"):
            core.set(k, None)
        core_sql = with_ctx(core, ctx)
        out = self.exec(core_sql)
        explain = {
            "UNION": "UNION：把兩邊的結果上下接起來" + ("，保留重複列。" if not q.args.get("distinct") else "，並去掉重複的列。"),
            "INTERSECT": "INTERSECT：只留下兩邊都有的列。",
            "EXCEPT": "EXCEPT：留下上方有、但下方沒有的列。",
        }.get(op, op)
        consume = op == "UNION"
        links = value_links(out, [(s.rows, s.ids) for s in sides], consume=consume)
        if op == "INTERSECT":
            links = [l + [[1, x[1]] for x in value_links(Rows(out.columns, [r]), [(sides[1].rows, sides[1].ids)], False)[0]]
                     for l, r in zip(links, out.rows)]
        steps = [{
            "kind": "setop", "clause": op, "title": op_text,
            "explain": explain + ("欄位依名稱對齊。" if q.args.get("by_name") else "兩邊依欄位位置對齊。"),
            "sql": gen(core_sql, pretty=True),
            "line": self.line_of(q.expression, r"\b(UNION|INTERSECT|EXCEPT)\b"),
            "panels": [sides[0].as_input(), sides[1].as_input(), panel(f"{op_text} 結果", out)],
            "anim": "match", "links": links,
        }]
        cur = Current(out, [f"u{i}" for i in range(len(out.rows))], f"{op_text} 結果")
        final_sql = final_sql or gen(with_ctx(q, ctx))
        final = final if final is not None else self.exec(final_sql)
        tail = TailSteps(self, q, ctx, cur, final, final_sql)
        steps += tail.build(order_query=lambda: _without(q, "limit", "offset"))
        return self.add_pipeline(title, depth, steps, tail.notes), final

    # ---- DML / DDL ------------------------------------------------------------
    def trace_dml(self, ast: exp.Expression):
        target = ast.this.this if isinstance(ast.this, exp.Schema) else ast.this
        if not isinstance(target, exp.Table):
            return self.trace_generic()
        tname = gen(exp.Table(this=target.this.copy(), db=target.args.get("db"), catalog=target.args.get("catalog")))
        verb = type(ast).__name__.upper()
        if isinstance(ast, exp.Insert) and isinstance(ast.expression, (exp.Subquery, *QUERY_TYPES)):
            self.safe_decompose(ast.expression, ast.args.get("with_"), "INSERT 的來源查詢", 1)
        try:
            before = self.exec(f"SELECT rowid AS __rowid, * FROM {tname}")
        except duckdb.Error:
            before = None
        steps = []
        where = ast.args.get("where")
        marks = None
        if before is not None and where is not None and not ast.args.get("from_") and not ast.args.get("using"):
            src = gen(target)
            try:
                probe = self.exec(f"SELECT rowid AS __rowid, *, ({gen(where.this)}) AS __pass FROM {src}")
                vis, meta = probe.split_meta()
                marks = meta["__pass"]
                steps.append({
                    "kind": "filter", "clause": "WHERE", "title": "WHERE " + short(self.src(where.this), 70),
                    "explain": f"先用 WHERE 找出要{'修改' if verb == 'UPDATE' else '刪除'}的列：條件為 TRUE 的列才會被影響，FALSE 或 NULL 的列不動。",
                    "sql": f"SELECT *, ({gen(where.this)}) AS matches FROM {src}", "line": self.line_of(where, r"\bWHERE\b"),
                    "panels": [panel(tname, vis, ids=[str(i) for i in meta["__rowid"]], marks=marks)],
                    "anim": "filter", "links": [],
                })
            except duckdb.Error:
                marks = None
        result = self.exec(self.text)
        after = None
        if before is not None:
            try:
                after = self.exec(f"SELECT rowid AS __rowid, * FROM {tname}")
            except duckdb.Error:
                after = None
        if before is not None and after is not None:
            steps.append(_diff_step(verb, tname, before, after, self.text, self.line_of(ast)))
        if result.columns and result.columns != ["Count"]:
            steps.append({
                "kind": "result", "clause": "RETURNING", "title": "RETURNING 結果",
                "explain": "RETURNING 回傳被影響的列。", "sql": self.text, "line": self.line_of(ast),
                "panels": [panel("RETURNING", result)], "anim": "reveal", "links": [],
            })
        if not steps:
            return self._message_step(verb, result)
        self.add_pipeline(verb, 0, steps)

    def trace_create(self, ast: exp.Create):
        self.safe_decompose(ast.expression, None, f"CREATE {ast.kind or ''} 的查詢".replace("  ", " "), 1)
        result = self.exec(self.text)
        target = ast.this.this if isinstance(ast.this, exp.Schema) else ast.this
        try:
            rows = self.exec(f"SELECT * FROM {gen(target)}")
        except duckdb.Error:
            return self._message_step("CREATE", result)
        self.add_pipeline("CREATE", 0, [{
            "kind": "result", "clause": "CREATE", "title": f"建立 {short(target)}",
            "explain": "用查詢結果建立新的資料表或 View，內容如下。",
            "sql": self.text, "line": self.line0 + 1,
            "panels": [panel(short(target), rows)], "anim": "reveal", "links": [],
        }])

    def trace_generic(self):
        result = self.exec(self.text)
        self._message_step(self.text.split(None, 1)[0].upper() if self.text else "SQL", result)

    def _message_step(self, verb: str, result: Rows):
        has_rows = bool(result.columns)
        self.add_pipeline(verb, 0, [{
            "kind": "result" if has_rows else "message", "clause": verb, "title": f"執行 {verb}",
            "explain": "這個指令沒有中間步驟，直接顯示執行結果。" if has_rows else "指令執行完成。",
            "sql": self.text, "line": self.line0 + 1,
            "panels": [panel("結果", result)] if has_rows else [], "anim": "reveal", "links": [],
        }])


def _returns_rows(session: Session, text: str) -> bool:
    return session.con.extract_statements(text)[-1].type == duckdb.StatementType.SELECT


def _has_modifiers(q: exp.Expression) -> bool:
    return any(q.args.get(k) for k in ("order", "limit", "offset"))


def _without(q: exp.Expression, *keys: str) -> exp.Expression:
    q = q.copy()
    for k in keys:
        q.set(k, None)
    return q


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _same_rows(a: Rows, b: Rows, ordered: bool) -> bool:
    sa = [row_sig(r) for r in a.rows]
    sb = [row_sig(r) for r in b.rows]
    return sa == sb if ordered else sorted(sa) == sorted(sb)


def _diff_step(verb, tname, before: Rows, after: Rows, sql, line) -> dict:
    bvis, bmeta = before.split_meta()
    avis, ameta = after.split_meta()
    bmap = {rid: r for rid, r in zip(bmeta["__rowid"], bvis.rows)}
    amap = {rid: r for rid, r in zip(ameta["__rowid"], avis.rows)}
    same_cols = bvis.columns == avis.columns
    before_status = ["deleted" if rid not in amap else ("changed" if same_cols and amap[rid] != r else "same")
                     for rid, r in zip(bmeta["__rowid"], bvis.rows)]
    after_status, changed = [], []
    for rid, r in zip(ameta["__rowid"], avis.rows):
        old = bmap.get(rid)
        if old is None:
            after_status.append("new")
            changed.append([])
        elif same_cols and old != r:
            after_status.append("changed")
            changed.append([i for i, (x, y) in enumerate(zip(old, r)) if x != y])
        else:
            after_status.append("same")
            changed.append([])
    counts = {k: after_status.count(k) for k in ("new", "changed")}
    counts["deleted"] = before_status.count("deleted")
    return {
        "kind": "dml", "clause": verb, "title": f"{verb} 前後比較",
        "explain": f"{tname}：新增 {counts['new']} 列、修改 {counts['changed']} 列、刪除 {counts['deleted']} 列。"
                   "綠色為新增、黃色為被修改（格子標出改動的欄位）、紅色為被刪除。",
        "sql": sql, "line": line,
        "panels": [
            panel(f"{tname}（執行前）", bvis, ids=[str(i) for i in bmeta["__rowid"]], role="input", status=before_status),
            panel(f"{tname}（執行後）", avis, ids=[str(i) for i in ameta["__rowid"]], status=after_status, changed=changed),
        ],
        "anim": "diff", "links": [],
    }


# ---------------------------------------------------------------------------
# SELECT decomposition
# ---------------------------------------------------------------------------
@dataclass
class Source:
    index: int
    alias: str  # "" when the source has no usable name
    node: exp.Expression  # possibly wrapped with a row-id column
    original: exp.Expression
    rid: bool  # carries __rid_<index>
    visible: bool  # columns appear in the joined row (False for SEMI/ANTI)
    columns: list[str] | None = None

    @property
    def rid_name(self) -> str:
        return f"{RID}{self.index}"


class SelectPipeline:
    MODES = ((True, True), (True, False), (False, True), (False, False))  # (lineage, prefix)

    def __init__(self, tr: StatementTracer, sel: exp.Select, ctx, title, depth, final, final_sql):
        self.tr, self.sel, self.ctx, self.title, self.depth = tr, sel, ctx, title, depth
        self.final, self.final_sql = final, final_sql

    def build(self):
        notes = self.subpipelines()
        if self.final is None:
            self.final_sql = gen(with_ctx(self.sel, self.ctx))
            self.final = self.tr.exec(self.final_sql)
        error = None
        for lineage, prefix in self.MODES:
            try:
                attempt = SelectAttempt(self, lineage, prefix)
                steps = attempt.run()
                pipe = self.tr.add_pipeline(self.title, self.depth, steps, notes + attempt.notes)
                pipe["mode"] = {"lineage": lineage, "prefix": prefix}
                if error is not None:
                    pipe["mode"]["degraded_by"] = f"{type(error).__name__}: {short(str(error), 200)}"
                return pipe, self.final
            except Exception as e:  # noqa: BLE001 - try the next, simpler mode
                error = e
        raise error

    def subpipelines(self) -> list[str]:
        """Decompose FROM-subqueries and uncorrelated expression subqueries first."""
        notes = []
        if self.depth >= MAX_DEPTH:
            return notes
        sources = [self.sel.args["from_"].this] if self.sel.args.get("from_") else []
        sources += [j.this for j in self.sel.args.get("joins") or []]
        for src in sources:
            if isinstance(src, exp.Subquery) and isinstance(src.this, QUERY_TYPES):
                self.tr.safe_decompose(src.this, self.ctx, f"子查詢 {src.alias or '(未命名)'}", self.depth + 1)
        for part in ("expressions", "where", "having", "qualify"):
            val = self.sel.args.get(part)
            for node in val if isinstance(val, list) else [val]:
                for sub in _direct_subqueries(node):
                    label = {"expressions": "SELECT", "where": "WHERE", "having": "HAVING", "qualify": "QUALIFY"}[part]
                    try:
                        self.tr.exec(exp.select("*").from_(exp.Subquery(this=with_ctx(sub, self.ctx))).limit(0))
                    except duckdb.Error:
                        notes.append(f"{label} 中的相關子查詢（correlated subquery）會針對每一列重新計算，無法單獨展開：{short(sub, 60)}")
                        continue
                    self.tr.safe_decompose(sub, self.ctx, f"{label} 中的子查詢", self.depth + 1)
        return notes


def _direct_subqueries(node):
    """Query nodes inside an expression whose nearest enclosing query is the outer one."""
    if node is None:
        return
    for child in node.iter_expressions():
        if isinstance(child, QUERY_TYPES):
            yield child
        elif isinstance(child, exp.Subquery) and isinstance(child.this, QUERY_TYPES):
            yield child.this
        else:
            yield from _direct_subqueries(child)


class SelectAttempt:
    def __init__(self, pipe: SelectPipeline, lineage: bool, prefix: bool):
        self.p = pipe
        self.tr = pipe.tr
        self.sel = pipe.sel
        self.ctx = pipe.ctx
        self.lineage = lineage
        self.prefix = prefix
        self.notes: list[str] = []
        self.steps: list[dict] = []

    # ---- query building -----------------------------------------------------
    def q(self, exprs, *, joins=None, where=True, group=False, having=False, qualify=False,
          distinct=False, order=False, limit=False) -> exp.Expression:
        q = exp.Select(expressions=[e.copy() for e in exprs])
        if self.sources:
            q.set("from_", exp.From(this=self.sources[0].node.copy()))
            js = self.joins if joins is None else self.joins[:joins]
            if js:
                q.set("joins", [j.copy() for j in js])
        if where and self.where is not None:
            q.set("where", exp.Where(this=self.where.copy()))
        if group and self.group is not None:
            q.set("group", self.group.copy())
        if having and self.having is not None:
            q.set("having", exp.Having(this=self.having.copy()))
        if self.sel.args.get("windows"):
            q.set("windows", [w.copy() for w in self.sel.args["windows"]])
        if qualify and self.qualify is not None:
            q.set("qualify", exp.Qualify(this=self.qualify.copy()))
        if distinct and self.sel.args.get("distinct"):
            q.set("distinct", self.sel.args["distinct"].copy())
        if order and self.sel.args.get("order"):
            q.set("order", self.sel.args["order"].copy())
        if limit:
            for k in ("limit", "offset"):
                if self.sel.args.get(k):
                    q.set(k, self.sel.args[k].copy())
        return with_ctx(q, self.ctx)

    def run_q(self, q) -> Rows:
        return self.tr.exec(q)

    def row_exprs(self, upto: int | None = None) -> list[exp.Expression]:
        """Projection that exposes every column of the joined row (plus row ids)."""
        srcs = self.sources if upto is None else self.sources[:upto]
        srcs = [s for s in srcs if s.visible]
        if not self.prefix:
            return [exp.Star()]
        out, rids = [], []
        for s in srcs:
            for c in s.columns:
                if c.startswith(RID):
                    continue
                out.append(alias(exp.column(c, table=s.alias, quoted=True), f"{s.alias}.{c}"))
            if s.rid:
                rids.append(alias(exp.column(s.rid_name, table=s.alias), s.rid_name))
        return out + rids

    def ids_of(self, meta: dict, n: int, upto: int | None = None) -> list[str]:
        srcs = self.sources if upto is None else self.sources[:upto]
        cols = [s.rid_name for s in srcs if s.visible and s.rid and s.rid_name in meta]
        if not cols:
            return self.fresh_ids(n)
        return [rid_key(vals) for vals in zip(*(meta[c] for c in cols))]

    def fresh_ids(self, n: int) -> list[str]:
        """Ids that match nothing in other steps (used when lineage is unavailable)."""
        self.uid = getattr(self, "uid", 0) + 1
        return [f"x{self.uid}_{i}" for i in range(n)]

    def subst(self, node: exp.Expression | None) -> exp.Expression | None:
        """Replace references to SELECT aliases (allowed by DuckDB) with their expressions."""
        if node is None:
            return None
        aliases = {e.alias.lower(): e.this for e in self.sel.expressions if isinstance(e, exp.Alias)}
        if not aliases:
            return node.copy()

        def repl(n):
            if (isinstance(n, exp.Column) and not n.table and n.name.lower() in aliases
                    and n.name.lower() not in self.source_cols):
                return exp.Paren(this=aliases[n.name.lower()].copy())
            return n

        return node.copy().transform(repl)

    # ---- setup ----------------------------------------------------------------
    def setup(self):
        sel = self.sel
        self.sources: list[Source] = []
        self.joins: list[exp.Join] = []
        if sel.args.get("from_"):
            raw = [(sel.args["from_"].this, None)] + [(j.this, j) for j in sel.args.get("joins") or []]
            for i, (node, join) in enumerate(raw):
                visible = join is None or join_type(join) not in ("SEMI", "ANTI")
                wrapped, has_rid, name = self.wrap(node, i)
                self.sources.append(Source(i, name, wrapped, node, has_rid, visible))
                if join is not None:
                    j = join.copy()
                    j.set("this", wrapped)
                    self.joins.append(j)
        if self.prefix:
            for s in self.sources:
                if not s.visible:
                    continue
                if not s.alias:
                    raise Unsupported("source without a name")
                probe = self.q([exp.Column(this=exp.Star(), table=exp.to_identifier(s.alias))], where=False)
                s.columns = self.run_q(probe.limit(0)).columns
        self.source_cols = set()
        if self.sources:
            self.source_cols = {c.lower() for c in self.run_q(self.q([exp.Star()], where=False).limit(0)).columns}
        self.where = self.subst(sel.args["where"].this) if sel.args.get("where") else None
        self.having = self.subst(sel.args["having"].this) if sel.args.get("having") else None
        self.qualify = self.subst(sel.args["qualify"].this) if sel.args.get("qualify") else None
        self.resolve_group()

    def wrap(self, node: exp.Expression, i: int):
        name = node.alias_or_name if isinstance(node, (exp.Table, exp.Subquery)) else node.alias
        if not self.lineage:
            return node.copy(), False, name
        rid = exp.alias_(exp.Window(this=exp.RowNumber()), f"{RID}{i}")
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier) and not node.args.get("pivots"):
            inner = node.copy()
            inner.set("alias", None)
            talias = node.args["alias"].copy() if node.args.get("alias") else exp.TableAlias(this=exp.to_identifier(node.name))
            return exp.Subquery(this=exp.select(exp.Star(), rid).from_(inner), alias=talias), True, name
        if isinstance(node, exp.Subquery) and isinstance(node.this, QUERY_TYPES) and not node.args.get("pivots"):
            old = node.args.get("alias")
            name = name or f"subquery{i}"
            inner_alias = exp.TableAlias(this=exp.to_identifier("__q"),
                                         columns=[c.copy() for c in (old.columns if old else [])] or None)
            inner = exp.Subquery(this=node.this.copy(), alias=inner_alias)
            return (exp.Subquery(this=exp.select(exp.Star(), rid).from_(inner),
                                 alias=exp.TableAlias(this=exp.to_identifier(name))), True, name)
        return node.copy(), False, name

    def resolve_group(self):
        sel = self.sel
        g = sel.args.get("group")
        self.aggs = collect_aggregates(
            [*sel.expressions, sel.args.get("having"), sel.args.get("order"), sel.args.get("qualify")])
        self.is_agg = bool(g) or bool(self.aggs) or sel.args.get("having") is not None
        self.keys: list[exp.Expression] = []
        self.complex_group = False
        self.group = None
        if not g:
            return
        items = []
        if g.args.get("all"):
            if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
                   for e in sel.expressions):
                raise Unsupported("GROUP BY ALL with *")
            items = [e.unalias() for e in sel.expressions
                     if not collect_aggregates([e]) and not collect_windows([e])]
        for e in g.expressions:
            if isinstance(e, exp.Literal) and not e.is_string:
                items.append(sel.expressions[int(e.this) - 1].unalias())
            elif isinstance(e, (exp.Rollup, exp.Cube, exp.GroupingSets)):
                self.complex_group = True
                for c in e.find_all(exp.Column):
                    if gen(c) not in {gen(x) for x in items}:
                        items.append(self.subst(c))
            else:
                items.append(self.subst(e))
        for extra in ("rollup", "cube", "grouping_sets"):  # DuckDB-style GROUP BY ROLLUP (...)
            for e in g.args.get(extra) or []:
                self.complex_group = True
                for c in e.find_all(exp.Column):
                    items.append(self.subst(c))
        self.keys = [k.copy() for k in items]
        if self.complex_group:
            self.group = g.copy()
        else:
            self.group = exp.Group(expressions=[k.copy() for k in self.keys]) if self.keys else g.copy()

    def key_rank(self) -> exp.Expression:
        order = exp.Order(expressions=[exp.Ordered(this=k.copy(), nulls_first=False) for k in self.keys])
        return alias(exp.Window(this=exp.Anonymous(this="DENSE_RANK"), order=order), "__grp")

    def key_cols(self) -> list[exp.Expression]:
        return [alias(exp.Paren(this=k.copy()), short(k, 40)) for k in self.keys]

    def agg_cols(self) -> list[exp.Expression]:
        return [alias(a.copy(), short(a, 40)) for a in self.aggs]

    def step(self, kind, clause, title, explain, q, panels, anim, line, links=None, notes=None) -> dict:
        st = {"kind": kind, "clause": clause, "title": title, "explain": explain,
              "sql": gen(q, pretty=True) if isinstance(q, exp.Expression) else q,
              "line": line, "panels": panels, "anim": anim, "links": links or []}
        if notes:
            st["notes"] = notes
        self.steps.append(st)
        return st

    # ---- the pipeline --------------------------------------------------------
    def run(self) -> list[dict]:
        self.setup()
        sel, tr = self.sel, self.tr
        cur: Current | None = None

        # FROM
        if self.sources:
            s0 = self.sources[0]
            q = self.q(self.row_exprs(1), joins=0, where=False)
            vis, meta = self.run_q(q).split_meta()
            cur = Current(vis, self.ids_of(meta, len(vis.rows), 1), "FROM 的結果")
            note = (["資料來自子查詢，展開過程見上方對應的流程。"]
                    if isinstance(s0.original, exp.Subquery) else None)
            s0_text = short(tr.src(s0.original, True, (TT.ON, TT.USING)), 60)
            self.step("source", "FROM", f"FROM {s0_text}",
                      "SQL 的執行從 FROM 開始：先把資料來源的每一列讀進來。", q,
                      [panel(s0_text, vis, ids=cur.ids)], "reveal",
                      tr.line_of(sel.args["from_"], r"\bFROM\b"), notes=note)

        # JOINs
        for j_index, join in enumerate(sel.args.get("joins") or []):
            src = self.sources[j_index + 1]
            jt = join_type(join)
            src_text = short(tr.src(src.original, True, (TT.ON, TT.USING)), 40)
            panels = [cur.as_input("左邊（目前累積的列）")]
            right_ids = None
            try:
                rq = self.single_source_query(src)
                rvis, rmeta = self.run_q(rq).split_meta()
                right_ids = ([str(v) for v in rmeta[src.rid_name]] if src.rid_name in rmeta
                             else self.fresh_ids(len(rvis.rows)))
                panels.append(panel(f"右邊：{src_text}", rvis, ids=right_ids, role="input"))
            except (duckdb.Error, Unsupported):
                panels.append(None)
            q = self.q(self.row_exprs(j_index + 2), joins=j_index + 1, where=False)
            vis, meta = self.run_q(q).split_meta()
            ids = self.ids_of(meta, len(vis.rows), j_index + 2)
            left_ids = self.ids_of(meta, len(vis.rows), j_index + 1)
            left_lineage = any(s.rid and s.visible and s.rid_name in meta for s in self.sources[:j_index + 1])
            links = []
            for n in range(min(len(vis.rows), DISPLAY_ROWS)):
                link = []
                if left_lineage and not all(p == "∅" for p in left_ids[n].split("|")):
                    link.append([0, left_ids[n]])
                if right_ids is not None and src.rid and src.rid_name in meta and meta[src.rid_name][n] is not None:
                    link.append([1, str(meta[src.rid_name][n])])
                links.append(link)
            if panels[1] is None:
                panels.pop()
                links = [[l for l in link if l[0] == 0] for link in links]
            panels.append(panel(f"{jt} JOIN 的結果", vis, ids=ids))
            cond = join.args.get("on") or join.args.get("using")
            ctext = f" ON {short(tr.src(cond, False, (TT.ON, TT.USING)), 50)}" if join.args.get("on") else (
                f" USING ({', '.join(short(c) for c in join.args['using'])})" if join.args.get("using") else "")
            self.step("join", "JOIN", (f", {src_text}" if jt == "COMMA" else f"{jt} JOIN {src_text}") + ctext,
                      JOIN_EXPLAIN[jt] + f"（{len(cur.rows.rows)} 列 × {panels[1]['total'] if len(panels) == 3 else '?'} 列 → {len(vis.rows)} 列）",
                      q, panels, "match", tr.line_of(join, r"\bJOIN\b|,"), links)
            cur = Current(vis, ids, "JOIN 後的列")

        # WHERE
        if self.where is not None and self.sources:
            q = self.q(self.row_exprs() + [alias(exp.Paren(this=self.where.copy()), "__pass")], where=False)
            vis, meta = self.run_q(q).split_meta()
            ids = self.ids_of(meta, len(vis.rows))
            marks = meta["__pass"]
            passed = sum(m is True for m in marks)
            self.step("filter", "WHERE", f"WHERE {short(tr.src(sel.args['where'].this), 70)}",
                      f"WHERE 逐列檢查條件：結果為 TRUE 的列留下，FALSE 或 NULL（未知）的列被丟掉。{len(marks)} 列中有 {passed} 列留下。",
                      q, [panel("逐列檢查 WHERE 條件", vis, ids=ids, marks=marks)], "filter",
                      tr.line_of(sel.args["where"], r"\bWHERE\b"))
            cur = Current(vis, ids, "WHERE 之後留下的列").subset(marks)

        # GROUP BY / aggregates / HAVING
        if self.is_agg:
            cur = self.aggregate_steps(cur)

        # window functions + QUALIFY
        windows = collect_windows([*sel.expressions, sel.args.get("qualify"), sel.args.get("order")])
        if windows and (self.sources or self.is_agg):
            cur = self.window_steps(cur, windows)

        # SELECT list
        cur = self.project_step(cur)

        # DISTINCT
        if sel.args.get("distinct"):
            cur = self.distinct_step(cur)

        tail = TailSteps(self.tr, sel, self.ctx, cur, self.p.final, self.p.final_sql, attempt=self)
        self.steps += tail.build(order_query=lambda: self.q(
            [self.star_safe(e) for e in sel.expressions], group=True, having=True, qualify=True, distinct=True, order=True))
        self.notes += tail.notes
        return self.steps

    def single_source_query(self, src: Source) -> exp.Expression:
        if isinstance(src.original, (exp.Lateral, exp.Unnest)) or (
                isinstance(src.original, exp.Table) and not isinstance(src.original.this, exp.Identifier)):
            raise Unsupported("depends on the left side")
        if self.prefix and src.visible:
            exprs = [alias(exp.column(c, table=src.alias, quoted=True), f"{src.alias}.{c}")
                     for c in src.columns if not c.startswith(RID)]
            if src.rid:
                exprs.append(alias(exp.column(src.rid_name, table=src.alias), src.rid_name))
        else:
            exprs = [exp.Star()]
        return with_ctx(exp.Select(expressions=exprs).from_(src.node.copy()), self.ctx)

    def aggregate_steps(self, cur: Current | None) -> Current:
        sel, tr = self.sel, self.tr
        g = sel.args.get("group")
        rank_ok = bool(self.keys) and not self.complex_group
        group_line = tr.line_of(g, r"\bGROUP\s+BY\b") if g else tr.line_of(sel, r"\bSELECT\b")
        grouped_input: dict | None = None
        if g is not None and self.keys and self.sources:
            q = self.q(self.key_cols() + self.row_exprs() + [self.key_rank()])
            rows = self.run_q(q)
            vis, meta = rows.split_meta()
            ids = self.ids_of(meta, len(vis.rows))
            order = sorted(range(len(vis.rows)), key=lambda i: (meta["__grp"][i] is None, meta["__grp"][i] or 0))
            vis = Rows(vis.columns, [vis.rows[i] for i in order], vis.overflow)
            ids = [ids[i] for i in order]
            groups = [meta["__grp"][i] for i in order]
            n_groups = len(set(groups))
            notes = (["ROLLUP / CUBE / GROUPING SETS 會用多種組合各分組一次；這裡以最細的組合上色，各組合的彙總結果見下一步。"]
                     if self.complex_group else None)
            grouped_input = panel("依分組鍵排好的列（同色 = 同一組）", vis, ids=ids, groups=groups,
                                  highlight=range(len(self.keys)), role="input")
            self.step("group", "GROUP BY", f"GROUP BY {short(tr.src(g.expressions) if g.expressions else 'ALL', 70)}",
                      f"GROUP BY 把分組鍵（最前面標色的欄位）相同的列歸成同一組，共 {n_groups} 組。之後每一組只會輸出一列。",
                      q, [dict(grouped_input, role="output")], "group", group_line, notes=notes)
        elif cur is not None:
            grouped_input = cur.as_input("所有列都屬於同一組", groups=[1] * len(cur.rows.rows))

        extra = [self.key_rank()] if rank_ok else []
        if self.having is not None:
            extra.append(alias(exp.Paren(this=self.having.copy()), "__pass"))
        q = self.q(self.key_cols() + self.agg_cols() + extra, group=True)
        vis, meta = self.run_q(q).split_meta()
        if rank_ok:
            groups = meta["__grp"]
        elif g is None:
            groups = [1] * len(vis.rows)
        else:
            groups = [None] * len(vis.rows)
        order = sorted(range(len(vis.rows)), key=lambda i: (groups[i] is None, groups[i] or 0))
        vis = Rows(vis.columns, [vis.rows[i] for i in order], vis.overflow)
        groups = [groups[i] for i in order]
        marks = [meta["__pass"][i] for i in order] if "__pass" in meta else None
        fresh = self.fresh_ids(len(groups))
        ids = [f"g{x}" if x is not None else fresh[n] for n, x in enumerate(groups)]
        agg_names = ", ".join(short(a, 30) for a in self.aggs) or "（沒有聚合函數）"
        explain = (f"每一組算出聚合函數 {agg_names}，壓縮成一列。" if g is not None
                   else f"沒有 GROUP BY 但用了聚合函數，所以整張表視為同一組，結果只有一列：{agg_names}。")
        panels = [grouped_input] if grouped_input else []
        panels.append(panel("每組一列", vis, ids=ids, groups=groups,
                            highlight=range(len(self.keys), len(self.keys) + len(self.aggs))))
        self.step("aggregate", "AGGREGATE", "計算聚合函數" + (f"：{short(agg_names, 60)}" if self.aggs else ""),
                  explain, q, panels, "aggregate", group_line)
        cur = Current(vis, ids, "分組彙總後的列")
        if marks is not None:
            h = sel.args["having"]
            passed = sum(m is True for m in marks)
            self.step("filter", "HAVING", f"HAVING {short(tr.src(h.this), 70)}",
                      f"HAVING 跟 WHERE 一樣是篩選，但它作用在「分組之後」的每一組上，所以可以用聚合函數。{len(marks)} 組中有 {passed} 組留下。",
                      q, [panel("逐組檢查 HAVING 條件", vis, ids=ids, groups=groups, marks=marks)], "filter",
                      tr.line_of(h, r"\bHAVING\b"))
            cur = cur.subset(marks)
        return cur

    def window_steps(self, cur: Current, windows: list[exp.Window]) -> Current:
        sel, tr = self.sel, self.tr
        if self.is_agg:
            base = self.key_cols() + self.agg_cols() + ([self.key_rank()] if self.keys and not self.complex_group else [])
        else:
            base = self.row_exprs()
        wcols = [alias(self.subst(w), short(w, 50)) for w in windows]
        extra = []
        part = windows[0].args.get("partition_by")
        if part:
            order = exp.Order(expressions=[exp.Ordered(this=self.subst(p), nulls_first=False) for p in part])
            extra.append(alias(exp.Window(this=exp.Anonymous(this="DENSE_RANK"), order=order), "__part"))
        if self.qualify is not None:
            extra.append(alias(exp.Paren(this=self.qualify.copy()), "__pass"))
        q = self.q(base + wcols + extra, group=self.is_agg, having=True)
        vis, meta = self.run_q(q).split_meta()
        if self.is_agg:
            ids = ([f"g{x}" for x in meta["__grp"]] if "__grp" in meta else
                   self.fresh_ids(len(vis.rows)) if self.sel.args.get("group") else ["g1"] * len(vis.rows))
        else:
            ids = self.ids_of(meta, len(vis.rows))
        groups = meta.get("__part")
        n_base = len(vis.columns) - len(wcols)
        self.step("window", "WINDOW", "計算視窗函數：" + short(", ".join(gen(w) for w in windows), 70),
                  "視窗函數（… OVER (…)）在 WHERE / GROUP BY / HAVING 之後計算。它不會把列壓縮成一列，而是替每一列算出一個值："
                  "PARTITION BY 決定跟哪些列一起算（同色 = 同一個分區），ORDER BY 決定分區內的順序。",
                  q, [panel("加上視窗函數欄位", vis, ids=ids, groups=groups, highlight=range(n_base, len(vis.columns)))],
                  "columns", tr.line_of(windows[0], None))
        cur = Current(vis, ids, "計算視窗函數後的列")
        if "__pass" in meta:
            marks = meta["__pass"]
            qn = sel.args["qualify"]
            self.step("filter", "QUALIFY", f"QUALIFY {short(tr.src(qn.this), 70)}",
                      f"QUALIFY 是「針對視窗函數結果的 WHERE」，在視窗函數算完後篩選列。{len(marks)} 列中有 {sum(m is True for m in marks)} 列留下。",
                      q, [panel("逐列檢查 QUALIFY 條件", vis, ids=ids, groups=groups, marks=marks,
                                highlight=range(n_base, len(vis.columns)))], "filter",
                      tr.line_of(qn, r"\bQUALIFY\b"))
            cur = cur.subset(marks)
        return cur

    def star_safe(self, e: exp.Expression) -> exp.Expression:
        """Hide the internal row-id columns from SELECT * / alias.*."""
        rid_sources = [s for s in getattr(self, "sources", []) if s.rid and s.visible]
        if not rid_sources:
            return e.copy()
        e = e.copy()
        if isinstance(e, exp.Star):
            e.set("except_", (e.args.get("except_") or []) + [exp.column(s.rid_name) for s in rid_sources])
        elif isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
            for s in rid_sources:
                if s.alias.lower() == e.table.lower():
                    star = e.this
                    star.set("except_", (star.args.get("except_") or []) + [exp.column(s.rid_name)])
        return e

    def project_step(self, cur: Current | None) -> Current:
        sel, tr = self.sel, self.tr
        proj = [self.star_safe(e) for e in sel.expressions]
        ident = []
        if self.is_agg:
            if self.keys and not self.complex_group:
                ident.append(self.key_rank())
        elif self.lineage:
            ident += [alias(exp.column(s.rid_name, table=s.alias if self.prefix else None), s.rid_name)
                      for s in self.sources if s.rid and s.visible]
        distinct = sel.args.get("distinct")
        if distinct is not None and distinct.args.get("on"):
            on = [self.subst(e) for e in distinct.args["on"].expressions]
            order = sel.args.get("order")
            win = exp.Window(this=exp.RowNumber(), partition_by=on,
                             order=self.resolve_order(order) if order else None)
            ident.append(alias(win, "__rn"))
        q = self.q(proj + ident, group=True, having=True, qualify=True)
        vis, meta = self.run_q(q).split_meta()
        if self.is_agg:
            ids = ([f"g{x}" for x in meta["__grp"]] if "__grp" in meta else
                   ["g1"] * len(vis.rows) if sel.args.get("group") is None else self.fresh_ids(len(vis.rows)))
        else:
            ids = self.ids_of(meta, len(vis.rows)) if self.sources else self.fresh_ids(len(vis.rows))
        known = set(cur.ids) if cur is not None else set()
        links = [[[0, i]] if i in known else [] for i in ids[:DISPLAY_ROWS]]
        panels = ([cur.as_input()] if cur is not None else []) + [panel("SELECT 的結果", vis, ids=ids)]
        self.rn = meta.get("__rn")
        self.step("project", "SELECT", "SELECT " + short(tr.src(list(sel.expressions)), 70),
                  "SELECT 在這麼後面才執行：它替每一列算出要輸出的欄位（計算式、別名、函數），不需要的欄位在這一步被捨棄。"
                  "這也是 WHERE 通常不能用 SELECT 別名的原因（DuckDB 例外地允許）。",
                  q, panels, "match" if cur is not None else "reveal",
                  tr.line_of(sel, r"\bSELECT\b") if sel.expressions else None, links if cur is not None else [])
        return Current(vis, ids, "SELECT 的結果")

    def resolve_order(self, order: exp.Order) -> exp.Order:
        out = []
        for o in order.expressions:
            o = o.copy()
            t = o.this
            if isinstance(t, exp.Literal) and not t.is_string:
                o.set("this", self.sel.expressions[int(t.this) - 1].unalias().copy())
            else:
                o.set("this", self.subst(t))
            out.append(o)
        return exp.Order(expressions=out)

    def distinct_step(self, cur: Current) -> Current:
        sel = self.sel
        on = sel.args["distinct"].args.get("on")
        if on:
            marks = [None if r is None else r == 1 for r in self.rn] if self.rn else None
            explain = "DISTINCT ON：每個 ON (…) 的值只保留一列，依 ORDER BY 排序後的第一列。"
        else:
            seen, marks = set(), []
            for r in cur.rows.rows:
                sig = row_sig(r)
                marks.append(sig not in seen)
                seen.add(sig)
            explain = "DISTINCT：完全相同的列只保留第一次出現的那一列（NULL 視為相同）。"
        if marks is None:
            self.notes.append("DISTINCT ON 無法標示保留哪些列，直接顯示結果。")
            return cur
        kept = sum(m is True for m in marks)
        self.step("filter", "DISTINCT", "DISTINCT" + (f" ON ({short(self.tr.src(list(on.expressions)), 50)})" if on else ""),
                  explain + f"{len(marks)} 列 → {kept} 列。", "-- 在 SELECT 結果上標記重複列",
                  [panel("標記重複的列", cur.rows, ids=cur.ids, marks=marks)], "filter",
                  self.tr.line_of(sel, r"\bDISTINCT\b"))
        return cur.subset(marks)


class TailSteps:
    """ORDER BY, LIMIT/OFFSET and the final result, shared by SELECT and set operations."""

    def __init__(self, tr: StatementTracer, q: exp.Expression, ctx, cur: Current, final: Rows, final_sql: str,
                 attempt: SelectAttempt | None = None):
        self.tr, self.qn, self.ctx, self.cur = tr, q, ctx, cur
        self.final, self.final_sql, self.attempt = final, final_sql, attempt
        self.notes: list[str] = []

    def build(self, order_query) -> list[dict]:
        tr, qn, cur = self.tr, self.qn, self.cur
        steps = []
        order = qn.args.get("order")
        if order is not None:
            oq = order_query()
            oq = oq if self.attempt is not None else with_ctx(oq, self.ctx)
            rows = tr.exec(oq)
            links = value_links(rows, [(cur.rows, cur.ids)])
            ids = [l[0][1] if l else f"o{i}" for i, l in enumerate(links)] + [f"o{i}" for i in range(len(links), len(rows.rows))]
            steps.append({
                "kind": "sort", "clause": "ORDER BY", "title": f"ORDER BY {short(tr.src(list(order.expressions)), 70)}",
                "explain": "ORDER BY 在 SELECT 之後執行，所以可以使用 SELECT 的別名。ASC 由小到大、DESC 由大到小；"
                           "DuckDB 預設把 NULL 排在最後。沒有 ORDER BY 時，結果的順序不保證。",
                "sql": gen(oq, pretty=True), "line": tr.line_of(order, r"\bORDER\s+BY\b"),
                "panels": [cur.as_input("排序前"), panel("排序後", rows, ids=ids)], "anim": "match", "links": links,
            })
            cur = Current(rows, ids, "排序後的列")
        limit, offset = qn.args.get("limit"), qn.args.get("offset")
        if limit is not None or offset is not None:
            off = 0
            if offset is not None:
                try:
                    off = int(tr.exec(f"SELECT ({gen(offset.expression)})").rows[0][0] or 0)
                except (duckdb.Error, TypeError, ValueError):
                    off = 0
            n = len(self.final.rows)
            marks = [off <= i < off + n for i in range(len(cur.rows.rows))]
            parts = [kw + " " + tr.src(x.expression) for kw, x in (("LIMIT", limit), ("OFFSET", offset)) if x is not None]
            steps.append({
                "kind": "filter", "clause": "LIMIT", "title": short(" ".join(parts), 70),
                "explain": (f"OFFSET 先跳過前 {off} 列，" if offset is not None else "")
                           + f"LIMIT 只取接下來的 {n} 列。LIMIT 最後才執行，所以通常搭配 ORDER BY 才有意義。",
                "sql": "-- 在排序後的列上標記保留範圍",
                "line": tr.line_of(limit or offset, r"\b(LIMIT|OFFSET)\b"),
                "panels": [panel("標記保留的列", cur.rows, ids=cur.ids, marks=marks)], "anim": "filter", "links": [],
            })
            cur = cur.subset(marks)
        result = {
            "kind": "result", "clause": "RESULT", "title": "最終結果",
            "explain": f"這就是整段查詢的輸出，共 {len(self.final.rows)} 列。",
            "sql": self.final_sql, "line": None,
            "panels": [panel("最終結果", self.final)], "anim": "reveal", "links": [], "notes": [],
        }
        if not (cur.rows.overflow or self.final.overflow):
            ordered = order is not None
            if not _same_rows(cur.rows, self.final, ordered):
                if _same_rows(cur.rows, self.final, False):
                    result["notes"].append("逐步結果與實際結果的列相同，但順序不同（沒有 ORDER BY 時順序不保證）。")
                else:
                    result["notes"].append("注意：逐步拆解的最後結果與實際執行結果不一致，可能是查詢含有隨機或時間函數；請以最終結果為準。")
        steps.append(result)
        return steps

import pytest

from sqlstepper.db import Session, to_json_value
from sqlstepper.engine import DISPLAY_ROWS, Tracer


@pytest.fixture(scope="session")
def session():
    return Session()


@pytest.fixture(scope="session")
def tracer(session):
    return Tracer(session)


def direct(session, sql):
    """Ground truth: DuckDB executing the statement itself."""
    with session.lock, session.deterministic():
        cur = session.con.execute(sql)
        return [d[0] for d in cur.description], [[to_json_value(v) for v in r] for r in cur.fetchall()]


def main_pipeline(result):
    return result["statements"][-1]["pipelines"][-1]


def all_steps(result):
    for st in result["statements"]:
        for p in st["pipelines"]:
            for s in p["steps"]:
                yield p, s


def produced(step):
    """Rows a step hands to the next step."""
    out = step["panels"][-1]
    if step["kind"] == "filter":
        return sum(m is True for m in out["marks"])
    return out["total"]


def consumed(step):
    """Rows a step starts from."""
    return step["panels"][0]["total"]


def check_invariants(result):
    """Structural checks every trace must satisfy."""
    for p in (p for st in result["statements"] for p in st["pipelines"]):
        steps = p["steps"]
        for i, s in enumerate(steps):
            for pan in s["panels"]:
                if pan["total"] <= DISPLAY_ROWS:
                    assert len(pan["rows"]) == pan["total"]
                    for key in ("ids", "marks", "groups", "status"):
                        if key in pan:
                            assert len(pan[key]) == pan["total"], (key, s["title"])
                for row in pan["rows"]:
                    assert len(row) == len(pan["columns"])
            for n, link in enumerate(s["links"]):
                for panel_index, rid in link:
                    target = s["panels"][panel_index]
                    assert target["role"] == "input"
                    assert rid in target["ids"], (s["title"], n, rid)
            # row counts must chain: what one step produces, the next consumes
            if i == 0 or s["kind"] in ("setop",) or p["title"].startswith("遞迴 CTE"):
                continue
            prev = steps[i - 1]
            if any(x["total"] > DISPLAY_ROWS or x["overflow"] for x in prev["panels"] + s["panels"]):
                continue
            if s["kind"] == "result":
                if prev["kind"] in ("filter", "sort", "project", "setop"):
                    assert produced(prev) == s["panels"][-1]["total"], (p["title"], prev["title"])
                continue
            if s["kind"] in ("join", "filter", "group", "aggregate", "window", "project", "sort"):
                assert consumed(s) == produced(prev), (p["title"], prev["title"], s["title"])

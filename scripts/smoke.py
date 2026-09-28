"""Print a compact outline of the steps produced for SQL given as args or the built-in examples."""

import sys

from sqlstepper.db import Session
from sqlstepper.engine import Tracer
from sqlstepper.samples import EXAMPLES


def outline(result: dict) -> None:
    for st in result["statements"]:
        for p in st["pipelines"]:
            print(f"  [{p['id']}] {p['title']} {p['notes'] or ''}")
            for x in p["steps"]:
                totals = [q["total"] for q in x["panels"] if q]
                print(f"     {x['kind']:<9} L{str(x['line']):<4} {x['title'][:60]:<60} panels={totals} "
                      f"links={len(x['links'])} {x.get('notes') or ''}")


def main() -> None:
    tracer = Tracer(Session())
    items = [{"title": "arg", "sql": a} for a in sys.argv[1:]] or EXAMPLES
    for ex in items:
        r = tracer.run(ex["sql"])
        print("==", ex["title"], ("ERROR: " + r["error"]) if r["error"] else "")
        outline(r)


if __name__ == "__main__":
    main()

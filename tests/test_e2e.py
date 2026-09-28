"""Drive the real UI in Microsoft Edge (skipped when Playwright / Edge are unavailable).

Set SQLSTEPPER_SHOTS=<dir> to save screenshots of each scenario.
"""

import os
import threading
from pathlib import Path

import pytest

from sqlstepper.db import Session
from sqlstepper.server import serve

sync_api = pytest.importorskip("playwright.sync_api")

SHOTS = os.environ.get("SQLSTEPPER_SHOTS")


@pytest.fixture(scope="module")
def url():
    httpd = serve(Session(), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="msedge")
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Edge not available: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser, url):
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(url)
    pg.wait_for_selector("#tables .tbl")
    pg.errors = errors
    yield pg
    assert not errors, errors
    ctx.close()


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def set_sql(page, sql):
    page.fill("#sql", sql)


def fastest(page):
    page.fill("#speed", "10")
    page.dispatch_event("#speed", "input")


def status(page):
    return page.inner_text("#status")


def test_loads_with_tables_and_examples(page):
    assert page.locator("#tables .tbl").count() == 6
    assert page.locator("#examples option").count() > 5
    assert "SQL Stepper" in page.inner_text("#stage")
    shot(page, "01-welcome")


def test_run_plays_to_the_end(page):
    fastest(page)
    set_sql(page, "SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id\n"
                  "WHERE o.status = 'shipped' ORDER BY o.id LIMIT 3")
    page.click("#run")
    page.wait_for_function("document.querySelector('#status').textContent.includes('執行完畢')", timeout=20000)
    clauses = page.locator("#plan .step .chip").all_inner_texts()
    assert clauses == ["FROM", "JOIN", "WHERE", "SELECT", "ORDER BY", "LIMIT", "RESULT"]
    assert page.locator("#stage .pnl.output tbody tr").count() == 3
    shot(page, "02-finished")


def test_line_breakpoint_then_row_by_row(page):
    fastest(page)
    set_sql(page, "SELECT *\nFROM customers\nWHERE city = 'Taipei'\nORDER BY id")
    page.click("#gutter .ln[data-line='3']")
    assert page.locator("#gutter .ln.bp").count() == 1
    page.click("#run")
    page.wait_for_function("document.querySelector('#status').textContent.includes('斷點')", timeout=20000)
    assert "WHERE" in page.inner_text(".stage-title")
    assert page.locator("#gutter .ln.current").get_attribute("data-line") == "3"
    marks = page.locator("#stage td.mark")
    assert marks.all_inner_texts() == [""] * 6  # paused before the step runs
    shot(page, "03-breakpoint")
    page.keyboard.press("F8")
    page.keyboard.press("F8")
    assert marks.all_inner_texts()[:3] == ["✓ TRUE", "✗ FALSE", ""]
    assert "第 2 / 6 列" in status(page)
    shot(page, "04-row-by-row")
    page.keyboard.press("F10")  # finish this step
    page.wait_for_function("document.querySelector('#status').textContent.includes('已完成')", timeout=10000)
    texts = marks.all_inner_texts()
    assert texts.count("✓ TRUE") == 2 and texts.count("? NULL") == 1  # David's city is NULL
    assert page.locator("#stage tr.dropped").count() == 4
    shot(page, "05-where-done")
    page.keyboard.press("F5")
    page.wait_for_function("document.querySelector('#status').textContent.includes('執行完畢')", timeout=20000)


def test_step_breakpoint_on_group_by_and_hover_lineage(page):
    fastest(page)
    set_sql(page, "SELECT status, COUNT(*) AS n\nFROM orders\nGROUP BY status\nORDER BY n DESC")
    page.click("#run")
    page.wait_for_function("document.querySelector('#status').textContent.includes('執行完畢')", timeout=20000)
    page.locator("#plan .step", has_text="AGGREGATE").locator(".sbp").click()
    assert page.locator("#plan .step.bp").count() == 1
    page.click("#run")
    page.wait_for_function("document.querySelector('#status').textContent.includes('斷點')", timeout=20000)
    assert "AGGREGATE" in page.inner_text(".stage-title")
    page.keyboard.press("F8")  # first group
    assert page.locator("#stage .pnl.input tr.hl-link").count() >= 1
    shot(page, "06-aggregate-group")
    page.keyboard.press("F10")
    page.wait_for_function("document.querySelector('#status').textContent.includes('已完成')", timeout=10000)
    # hover an output row: its member rows light up
    page.locator("#stage .pnl.output tbody tr").first.hover()
    assert page.locator("#stage .pnl.input tr.hl-link").count() >= 1


def test_join_animation_highlights_sources(page):
    page.fill("#speed", "3")
    page.dispatch_event("#speed", "input")
    set_sql(page, "SELECT * FROM customers c\nLEFT JOIN orders o ON o.customer_id = c.id")
    page.click("#gutter .ln[data-line='2']")
    page.click("#run")
    page.wait_for_function("document.querySelector('#status').textContent.includes('斷點')", timeout=20000)
    page.keyboard.press("F8")
    page.keyboard.press("F8")
    assert page.locator("#stage .panels.three").count() == 1
    assert page.locator("#stage .pnl.input tr.hl-link").count() == 2  # one left row + one right row
    shot(page, "07-join")


def test_plan_click_navigation_and_prev(page):
    fastest(page)
    set_sql(page, "SELECT name FROM products WHERE price > 100 ORDER BY price")
    page.click("#run")
    page.wait_for_function("document.querySelector('#status').textContent.includes('執行完畢')", timeout=20000)
    page.locator("#plan .step", has_text="WHERE").click()
    assert "WHERE" in page.inner_text(".stage-title")
    page.click("#prev")
    assert "FROM" in page.inner_text(".stage-title")


def test_editing_marks_trace_stale(page):
    fastest(page)
    set_sql(page, "SELECT 1")
    page.click("#run")
    page.wait_for_function("document.querySelector('#status').textContent.includes('執行完畢')", timeout=20000)
    page.type("#sql", " + 1")
    assert "已修改" in status(page)
    assert page.is_disabled("#next")


def test_error_is_shown(page):
    set_sql(page, "SELECT 1;\nSELECT * FROM missing_table;")
    page.click("#run")
    page.wait_for_selector(".errcard", timeout=20000)
    assert "missing_table" in page.inner_text(".errcard")
    shot(page, "08-error")


def test_import_csv_via_file_picker(page, tmp_path):
    f = tmp_path / "scores.csv"
    f.write_text("student,score\nAmy,90\nBen,72\n", encoding="utf-8")
    page.once("dialog", lambda d: d.accept("scores"))
    page.set_input_files("#file", str(f))
    page.wait_for_selector("#tables .tbl-name:text('scores')", timeout=10000)
    fastest(page)
    set_sql(page, "SELECT AVG(score) FROM scores")
    page.click("#run")
    page.wait_for_function("document.querySelector('#status').textContent.includes('執行完畢')", timeout=20000)
    assert "81" in page.inner_text("#stage .pnl.output")


def test_dml_diff_and_examples_dropdown(page):
    fastest(page)
    page.once("dialog", lambda d: d.accept())
    last = page.locator("#examples option").count() - 2  # the DML example
    page.select_option("#examples", str(last))
    page.wait_for_function("document.querySelector('#status').textContent.includes('執行完畢')", timeout=30000)
    page.locator("#plan .step", has_text="UPDATE 前後比較").click()
    assert page.locator("#stage tr.st-changed").count() == 4  # 2 rows before + 2 rows after
    assert page.locator("#stage td.cell-changed").count() == 2
    shot(page, "09-dml")


def test_dark_theme_and_small_screen(page):
    page.click("#theme")
    assert page.evaluate("document.documentElement.dataset.theme") in ("dark", "light")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    shot(page, "10-mobile")

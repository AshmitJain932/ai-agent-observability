"""Dashboard in a real browser (Playwright + headless Chromium): states, navigation, rendering, safety.

Needs Chromium for Playwright. In a fresh environment: `uv run playwright install chromium`. If no
browser can be launched these tests are skipped with that instruction (the API tests still run).
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from aobs_server.dashboard import DashboardConfig, create_app
from support import REPO_ROOT

playwright_api = pytest.importorskip("playwright.sync_api")

SAMPLES = REPO_ROOT / "examples" / "diagnose"
TOKEN = "ui-test-token-abcdefghijklmnop"
SENSITIVE = ["jane.doe@example.com", "Berlin warehouse", "Summarize the account", "Jane has two", "c-7781"]
FALLBACK_CHROMIUM = Path("/opt/pw-browsers/chromium")


class LiveServer:
    def __init__(self, config: DashboardConfig) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=self.port,
                                                    log_level="warning"))  # fmt: skip
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> LiveServer:
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("dashboard server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def url(self, fragment: str = "") -> str:
        return f"http://127.0.0.1:{self.port}/{fragment}"


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with playwright_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception:
            if not FALLBACK_CHROMIUM.exists():
                pytest.skip("Chromium for Playwright is not installed: run `uv run playwright install chromium`")
            instance = p.chromium.launch(executable_path=str(FALLBACK_CHROMIUM))
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def samples_server() -> Iterator[LiveServer]:
    with LiveServer(DashboardConfig(samples_dir=SAMPLES)) as server:
        yield server


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    problems: list[str] = []
    pg.on("pageerror", lambda e: problems.append(f"page error: {e}"))
    pg.on(
        "console",
        lambda m: problems.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None,
    )
    pg.problems = problems
    yield pg
    context.close()
    assert problems == [], problems  # no script errors and no Content-Security-Policy violations


def main_text(pg: Any) -> str:
    return str(pg.inner_text("#main"))


# --------------------------------------------------------------------------- overview and states


def test_overview_shows_sample_data_kpis(page: Any, samples_server: LiveServer) -> None:
    page.goto(samples_server.url("#/sample-repeated/overview"))
    page.wait_for_selector("#kpis")
    assert page.inner_text("#source-badge").lower() == "sample data"
    assert "not live production traffic" in page.inner_text("#source-note")
    kpis = page.inner_text("#kpis")
    for expected in ("Total runs\n63", "Failed\n7", "Failure rate\n11%", "sum over 57 of 63 runs"):
        assert expected in kpis
    assert page.locator("main table tbody tr").count() == 8


def test_loading_state_is_shown_while_waiting(page: Any, samples_server: LiveServer) -> None:
    release = threading.Event()

    def slow(route: Any) -> None:
        release.wait(5)
        route.continue_()

    page.route("**/api/datasets/sample-pairs/overview", slow)
    page.goto(samples_server.url("#/sample-pairs/overview"), wait_until="domcontentloaded")
    page.wait_for_selector("text=Loading overview…")
    release.set()
    page.wait_for_selector("#kpis")


def test_api_error_shows_an_error_state_with_retry(page: Any, samples_server: LiveServer) -> None:
    body = json.dumps({"type": "urn:aobs:problem:internal-error", "title": "Internal server error", "status": 500,
                       "detail": "The dashboard could not process this request"})  # fmt: skip
    page.route("**/api/datasets/sample-pairs/overview",
               lambda route: route.fulfill(status=500, body=body, content_type="application/problem+json"))  # fmt: skip
    page.goto(samples_server.url("#/sample-pairs/overview"))
    alert = page.wait_for_selector("[role=alert]")
    assert "Request failed (500)" in alert.inner_text() and "could not process" in alert.inner_text()
    page.unroute("**/api/datasets/sample-pairs/overview")
    page.click("text=Retry")
    page.wait_for_selector("#kpis")


def test_unreachable_server_shows_a_clear_message(page: Any, samples_server: LiveServer) -> None:
    page.route("**/api/datasets/sample-pairs/runs*", lambda route: route.abort())
    page.goto(samples_server.url("#/sample-pairs/runs"))
    alert = page.wait_for_selector("[role=alert]")
    assert "Could not reach the dashboard server" in alert.inner_text()


def test_empty_local_directory_and_token_prompt(page: Any, browser: Any, tmp_path: Path) -> None:
    with LiveServer(DashboardConfig(samples_dir=None, data_dir=tmp_path, token=TOKEN)) as server:
        page.goto(server.url("#/local/overview"))
        page.wait_for_selector("#token-input")  # no token yet: asked for it, nothing shown
        assert "needs the dashboard token" in main_text(page)
        page.fill("#token-input", TOKEN)
        page.click("text=Open")
        page.wait_for_selector("text=No runs in this dataset yet")
        assert page.inner_text("#source-badge").lower() == "local files"


def test_token_in_the_url_fragment_is_used_and_removed(page: Any, tmp_path: Path) -> None:
    shutil.copy(SAMPLES / "traces" / "refund_outage.jsonl", tmp_path / "outage.jsonl")
    with LiveServer(DashboardConfig(samples_dir=None, data_dir=tmp_path, token=TOKEN)) as server:
        page.goto(server.url(f"#token={TOKEN}"))
        page.wait_for_selector("#kpis")
        assert TOKEN not in page.url
        assert "Total runs\n1" in page.inner_text("#kpis")


# --------------------------------------------------------------------------- runs and details


def test_runs_explorer_filters_and_opens_details(page: Any, samples_server: LiveServer) -> None:
    page.goto(samples_server.url("#/sample-failures/runs"))
    page.wait_for_selector("tbody tr")
    assert page.locator("tbody tr").count() == 5
    page.select_option("#filter-status", "failed")
    playwright_api.expect(page.locator("tbody tr")).to_have_count(2)  # (string eval is blocked by the page's CSP)
    assert "status=failed" in page.url
    page.click("tbody a:has-text('restock_check')")
    page.wait_for_selector("#execution")
    findings = page.inner_text("#findings")
    assert "tool 'search_inventory' failed 3 times with ConnectionError" in findings
    assert "Retries did not help" in findings and "[REDACTED:url_credentials]" in findings
    assert page.locator("#execution tbody tr").count() == 5
    assert page.locator("#execution .bar.error").count() == 4
    assert page.inner_text("#run-tokens") == "not reported by the provider"
    assert page.inner_text("#run-cost") == "not reported"


def test_run_details_never_show_content(page: Any, samples_server: LiveServer) -> None:
    page.goto(samples_server.url("#/sample-failures/runs"))
    page.click("tbody a:has-text('summarize_account')")
    page.wait_for_selector("#execution")
    text = page.inner_text("body")
    assert "waste.repeated_call" in text and "latency.bottleneck" in text
    for secret in SENSITIVE:
        assert secret not in text, secret


def test_trace_values_are_rendered_as_text_not_markup(page: Any, tmp_path: Path) -> None:
    batch = json.loads((SAMPLES / "traces" / "refund_outage.jsonl").read_text().splitlines()[0])
    payload = '<img src=x onerror="window.__xss=1">'
    for event in batch["events"]:
        if event.get("parent_span_id") is None:
            event["name"] = payload
    (tmp_path / "evil.jsonl").write_text(json.dumps(batch) + "\n")
    with LiveServer(DashboardConfig(samples_dir=None, data_dir=tmp_path, token=TOKEN)) as server:
        page.goto(server.url(f"#token={TOKEN}"))
        page.wait_for_selector("#kpis")
        page.goto(server.url("#/local/runs"))
        page.wait_for_selector("tbody tr")
        assert payload in main_text(page)
        page.click("tbody td a")
        page.wait_for_selector("#execution")
        assert page.locator("main img").count() == 0
        assert page.evaluate("window.__xss") is None


# --------------------------------------------------------------------------- regression analysis


def test_compare_two_runs_preset(page: Any, samples_server: LiveServer) -> None:
    page.goto(samples_server.url("#/sample-pairs/compare"))
    page.click("[data-preset=refund-ok-vs-outage]")
    page.wait_for_selector("#compare-result")
    result = page.inner_text("#compare-result")
    assert "regression.run_failed" in result and "regression.new_error" in result
    assert "Possible explanations" in result and "Not verified" in result
    assert "SAMPLE DATA" in page.inner_text("#source-badge").upper()


def test_compare_groups_preset_and_form(page: Any, samples_server: LiveServer) -> None:
    page.goto(samples_server.url("#/sample-repeated/compare"))
    page.click("[data-preset=healthy-vs-slow]")
    page.wait_for_selector("#groups-result")
    duration = page.inner_text("tr[data-metric='run.duration']")
    assert "likely regression" in duration
    page.click("[data-preset=healthy-vs-few]")
    playwright_api.expect(page.locator("tr[data-metric='run.duration']")).to_contain_text("insufficient data")
    # pick groups by hand: baseline vs healthy rerun -> no meaningful change
    page.uncheck("#group-current input[value='few_runs.jsonl']")
    page.check("#group-current input[value='healthy_rerun.jsonl']")
    page.click("#compare-go")
    page.wait_for_selector("text=No meaningful change detected in any measured metric.")


def test_compare_errors_are_shown(page: Any, samples_server: LiveServer) -> None:
    # two runs from a file holding ten: the API asks for a run selection, and the page says so
    page.goto(samples_server.url("#/sample-repeated/compare?mode=runs&baseline=baseline.jsonl&current=slow.jsonl"))
    alert = page.wait_for_selector("[role=alert]")
    assert "Request failed (400)" in alert.inner_text() and "holds 10 runs" in alert.inner_text()

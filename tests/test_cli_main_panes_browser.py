"""Browser check: call site shows full caller; definition shows callee.

Requires a local FastAPI index under ~/.codeview (skipped otherwise).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8767"
DB = Path.home() / ".codeview/indexes/home__alexander__.codeview__repos__https_github.com_fastapi_fastapi.git-3a4d7ae11b.sqlite3"
REPO = Path.home() / ".codeview/repos/https_github.com_fastapi_fastapi.git-3a4d7ae11b"


def api(path: str, data=None):
    if data is None:
        return json.load(urllib.request.urlopen(BASE + path, timeout=10))
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=10))


def wait_ready() -> None:
    for _ in range(50):
        try:
            api("/api/stats")
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


@pytest.mark.skipif(not DB.is_file() or not REPO.is_dir(), reason="local fastapi index required")
def test_call_site_shows_caller_body_and_callee_definition():
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
from pathlib import Path
import uvicorn
from codeview.server.app import create_app
from codeview.service import ExplorerService
s = ExplorerService()
s.open(Path({str(DB)!r}))
s.store.set_meta("root", {str(REPO)!r})
uvicorn.run(create_app(s), host="127.0.0.1", port=8767, log_level="error")
""",
        ],
        cwd=str(ROOT),
    )
    try:
        wait_ready()
        tree = api("/api/tree?path=")
        main_sym = next(
            s
            for s in tree["results"]
            if s["name"] == "main" and s["location"]["path"].endswith("fastapi/cli.py")
        )
        callees = api("/api/expand", {"symbol_id": main_sym["id"], "kind": "calls"})
        names = [r["to_symbol"]["name"] for r in callees["relations"] if r.get("to_symbol")]
        assert "cli_main" in names

        # API: caller body + callee binding
        call_snip = api(
            f"/api/source/{main_sym['id']}?line=13&path=fastapi/cli.py&span=body"
        )
        assert "def main" in call_snip["text"]
        assert "cli_main()" in call_snip["text"]
        assert call_snip["highlight_line"] == 13
        assert call_snip["start_line"] == main_sym["location"]["line"]

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(BASE + "/")
            page.wait_for_selector("#tree .sym")
            page.locator("#tree .sym", has_text="main").filter(has_text="cli.py").first.locator(
                "xpath=ancestor::div[contains(@class,'row')]//button[contains(@class,'toggle')]"
            ).click()
            page.wait_for_timeout(700)
            page.locator("#tree .sym", has_text="cli_main").first.click()
            page.wait_for_timeout(500)

            call_text = page.locator("#srcCall").inner_text()
            def_text = page.locator("#srcDef").inner_text()
            def_label = page.locator("#srcDefLabel").inner_text()

            assert page.locator("#srcCallBlock").get_attribute("hidden") is None
            assert "def main" in call_text
            assert "cli_main()" in call_text.replace(" ", "")
            assert page.locator("#srcCall .line.hl").count() == 1
            assert "cli_main()" in page.locator("#srcCall .line.hl").inner_text().replace(" ", "")
            assert call_text.strip() != def_text.strip()
            assert "from fastapi_cli" in def_text
            assert "def main" not in def_text
            assert "no definition body" in def_text.lower()
            assert "unresolved" in def_label.lower() or "external" in def_label.lower()
            # Definition pane must not be a paste of the call-site body.
            assert "raise RuntimeError" not in def_text
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

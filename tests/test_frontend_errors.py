from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest


def test_browser_formats_structured_api_errors_as_readable_text():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the browser error formatter regression test.")
    source = (Path(__file__).parents[1] / "frontend" / "index.html").read_text(encoding="utf-8")
    match = re.search(r"function apiErrorMessage\(detail\)\{[^}]*\}[^\n]*", source)
    assert match, "The browser API error formatter is missing."
    formatter = match.group(0)
    script = formatter + r'''
const assert = require("node:assert/strict");
assert.equal(apiErrorMessage("Folder unavailable"), "Folder unavailable");
assert.equal(apiErrorMessage([{loc:["body","path"],msg:"Field required",type:"missing"}]), "Field required");
assert.equal(apiErrorMessage({message:"Permission denied"}), "Permission denied");
assert.ok(!apiErrorMessage({detail:"bad"}).includes("[object Object]"));
'''
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_browser_inline_javascript_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the browser JavaScript syntax check.")
    source = (Path(__file__).parents[1] / "frontend" / "index.html").read_text(encoding="utf-8")
    script = re.search(r"<script>([\s\S]*?)</script>", source)
    assert script, "The browser application script is missing."
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as file:
        file.write(script.group(1))
        name = file.name
    try:
        subprocess.run([node, "--check", name], check=True, capture_output=True, text=True)
    finally:
        Path(name).unlink(missing_ok=True)

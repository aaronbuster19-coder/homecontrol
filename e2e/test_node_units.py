"""The plain-Node unit tests (tests/*.test.js: snapping and wall maths) as part of the same run."""
import os
import shutil
import subprocess

import pytest

from conftest import ROOT


def test_node_unit_tests():
    if not shutil.which("node"):
        if os.environ.get("E2E_REQUIRE_NODE") == "1":  # CI: never skip silently
            pytest.fail("node not installed")
        pytest.skip("node not installed")
    files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("*.test.js"))
    assert files
    r = subprocess.run(["node", "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr

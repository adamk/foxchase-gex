import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_presence_ui_dom_harness():
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "test_presence_ui.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "PRESENCE_UI_PASS" in result.stdout

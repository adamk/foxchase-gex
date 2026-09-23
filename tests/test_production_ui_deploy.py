import hashlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
PRODUCTION_UI = ROOT / "deploy" / "production-ui"
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy_production_ui.sh"
DOM_HARNESS = ROOT / "tests" / "test_dom_harness.js"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_production_ui_source_manifest_matches_files():
    result = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"],
        cwd=PRODUCTION_UI,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_production_ui_source_has_reviewed_final_hashes():
    assert sha256(PRODUCTION_UI / "index.html") == "a32a780ac1d39a164ecc1fdae340d790ef08b3c13ee59d8cfe6d82ab22767895"
    assert sha256(PRODUCTION_UI / "app.js") == "b962be3de925f64566564e5af2bb831fb45d092bd601c324d71caa04694e5caf"
    assert sha256(PRODUCTION_UI / "style.css") == "6d478c440a151e7b0b186e66164c2ad02ae8203c8b01beaf76a77e764e0559f2"


def test_deployment_dry_run_is_read_only_and_maps_only_three_files():
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY RUN: no production changes" in result.stdout
    assert result.stdout.count(" -> /opt/foxchase-gex/static/") == 3
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "> /SHA256SUMS" not in script
    assert ">> /SHA256SUMS" not in script
    assert '"${backup_dir}/SHA256SUMS"' in script


def test_production_ui_preserves_final_density_and_chart_classification_read():
    template = (PRODUCTION_UI / "index.html").read_text(encoding="utf-8")
    script = (PRODUCTION_UI / "app.js").read_text(encoding="utf-8")
    styles = (PRODUCTION_UI / "style.css").read_text(encoding="utf-8")
    assert "const CHART_SYMBOLS = Object.freeze([\"NDX\", \"SPX\"]);" in script
    assert "const GEX_BAR_GAP = 0.08;" in script
    assert 'tickmode: "array"' in script
    assert "const SCALE_STORAGE_KEY" in script
    assert "localStorage" in script
    assert "fetch(`/api/gex/${state.symbol}`" in script
    assert "setInterval" in script
    assert "patterns?.primary" in script
    assert "pattern.signals" not in script
    assert "pattern.action_read" not in script
    assert "pattern.summary" not in script
    assert "pattern.key_read" not in script
    assert "read_title" not in script
    assert "pattern-box" not in template
    assert "Foxchase Read" not in template
    assert "pattern-box" not in script
    assert "Foxchase Read" not in script
    assert "pattern-box" not in styles
    assert "pattern-signals" not in styles
    assert template.count('class="ramp-schematic"') == 6
    assert template.count('class="chart-classification"') == 2
    assert 'id="classification-NDX"' in template
    assert 'id="classification-SPX"' in template
    assert template.count('class="chart-heading-row"') == 2
    assert "ramp-active" not in template
    assert "data-category=" not in template
    assert "is-active-NDX" not in script
    assert "is-active-SPX" not in script
    assert "ramp-active" not in script
    assert "ramp-active" not in styles
    for number, category in enumerate((
        "Gamma Pin",
        "Mixed Gamma",
        "Forward Positive Ramp",
        "Backward Positive Ramp",
        "Forward Negative Ramp",
        "Backward Negative Ramp",
    )):
        assert f'>{number}</span>' in template
        assert category in template
    classification_styles = styles[styles.index(".chart-classification"):styles.index(".chart-kind")]
    assert "#d8a900" not in classification_styles
    assert "background: #242424" in classification_styles
    assert "border: 1px solid #555" in classification_styles
    for primary, semantic_class, color in (
        ("gamma pin", "chart-classification--gamma-pin", "#ddd"),
        ("mixed gamma", "chart-classification--mixed", "#d8bf00"),
        ("forward positive ramp", "chart-classification--positive", "#27b46e"),
        ("backward positive ramp", "chart-classification--positive", "#27b46e"),
        ("forward negative ramp", "chart-classification--negative", "#e04b4b"),
        ("backward negative ramp", "chart-classification--negative", "#e04b4b"),
    ):
        assert f'"{primary}": "{semantic_class}"' in script
        assert f".{semantic_class}" in classification_styles
        assert f"color: {color}" in classification_styles
    assert "chart-classification--neutral" in script
    assert ".chart-classification--neutral" in classification_styles
    assert "color: #999" in classification_styles
    assert "patterns?.primary" in script


def test_production_dom_harness_covers_independent_header_classification_state():
    result = subprocess.run(
        ["node", str(DOM_HARNESS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "DOM_HARNESS_PASS" in result.stdout

import hashlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
PRODUCTION_UI = ROOT / "deploy" / "production-ui"
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy_production_ui.sh"


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
    assert sha256(PRODUCTION_UI / "index.html") == "34c12bc6ea488b4ebca29bb131aca86bc1253c9c0470bdc45eceeea4a4345452"
    assert sha256(PRODUCTION_UI / "app.js") == "d7a79d6fb89a85f480fccb410eb22172c9cadfdec22ec6c986871c75a81b0b30"
    assert sha256(PRODUCTION_UI / "style.css") == "5874421a2484cf615284e59ef45df5b78dfbd6a5ec345494bea2e6d7b5487b3c"


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


def test_production_ui_preserves_final_density_and_concise_read():
    script = (PRODUCTION_UI / "app.js").read_text(encoding="utf-8")
    assert "const GEX_BAR_GAP = 0.08;" in script
    assert 'tickmode: "array"' in script
    assert "pattern.action_read || pattern.summary" in script
    assert "pattern.key_read" not in script
    assert "pattern-signals" in script

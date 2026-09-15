import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "gex_client" / "templates" / "index.html"
SCRIPT = ROOT / "gex_client" / "static" / "js" / "app.js"
STYLES = ROOT / "gex_client" / "static" / "css" / "style.css"


def test_dashboard_renders_ndx_then_spx_with_independent_controls():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.index('data-symbol="NDX"') < html.index('data-symbol="SPX"')
    assert 'id="gexChart-NDX"' in html
    assert 'id="gexChart-SPX"' in html
    assert 'id="scale-NDX"' in html
    assert 'id="scale-SPX"' in html
    assert 'id="history-session-NDX"' in html
    assert 'id="history-session-SPX"' in html
    assert "Gamma Pin" in html
    assert "Forward Positive Ramp" in html
    assert "Backward Negative Ramp" in html
    assert html.count('class="ramp-schematic"') == 6
    assert 'aria-label="Gamma Pin schematic"' in html
    assert 'aria-label="Backward Negative Ramp schematic"' in html


def test_dashboard_css_has_wide_grid_and_stacked_breakpoint():
    css = STYLES.read_text(encoding="utf-8")
    assert ".chart-grid" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert "@media (max-width: 900px)" in css
    assert ".chart-grid { grid-template-columns: 1fr; }" in css
    assert "grid-template-rows: auto 78px" in css
    assert ".ramp-price-line" in css
    assert "min-height: 720px" in css
    assert "height: min(860px, calc(100vh - 90px))" in css


def test_scale_math_is_symmetric_and_auto_has_padding():
    script = f"""
const ui = require({json.dumps(str(SCRIPT))});
const auto = {{scale: "auto", autoEdge: null}};
const autoRange = ui.resolveScaleRange({{strikes: [{{gex: -10}}, {{gex: 5}}]}}, auto);
const stableRange = ui.resolveScaleRange({{strikes: [{{gex: 9}}]}}, auto);
const expandedRange = ui.resolveScaleRange({{strikes: [{{gex: 20}}]}}, auto);
const fixedRange = ui.resolveScaleRange({{strikes: [{{gex: 1}}]}}, {{scale: "100"}});
console.log(JSON.stringify({{autoRange, stableRange, expandedRange, fixedRange, options: ui.SCALE_OPTIONS}}));
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True
    )
    values = json.loads(result.stdout)
    assert values["autoRange"] == [-12, 12]
    assert values["stableRange"] == [-12, 12]
    assert values["expandedRange"] == [-24, 24]
    assert values["fixedRange"] == [-100, 100]
    assert values["options"]["NDX"][-1] == 1000
    assert values["options"]["SPX"][0] == 5


def test_forward_and_backward_positive_ramps_are_visual_opposites():
    html = TEMPLATE.read_text(encoding="utf-8")

    def widths(label):
        marker = f'aria-label="{label} schematic">'
        start = html.index(marker)
        end = html.index("</svg>", start)
        block = html[start:end]
        return {
            int(y): int(width)
            for y, width in re.findall(r'class="ramp-bar-positive" x="80" y="(\d+)" width="(\d+)"', block)
        }

    assert widths("Forward Positive Ramp") == {29: 16, 19: 31, 9: 52}
    assert widths("Backward Positive Ramp") == {9: 16, 19: 31, 29: 52}


def test_gamma_pin_has_one_dominant_near_price_bar():
    html = TEMPLATE.read_text(encoding="utf-8")
    marker = 'aria-label="Gamma Pin schematic">'
    start = html.index(marker)
    end = html.index("</svg>", start)
    block = html[start:end]
    widths = {
        int(y): int(width)
        for y, width in re.findall(
            r'class="ramp-bar-(?:positive|negative)" x="\d+" y="(\d+)" width="(\d+)"',
            block,
        )
    }

    assert widths == {28: 12, 35: 72, 43: 14, 50: 10}
    assert max(widths.values()) == widths[35]
    assert all(width <= 14 for y, width in widths.items() if y != 35)

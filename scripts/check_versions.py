from __future__ import annotations

import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]


def _match(pattern: str, text: str, description: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise SystemExit(f"Could not find {description}")
    return match.group(1)


manifest_version = json.loads(
    (ROOT / "custom_components/gui_recorder/manifest.json").read_text(encoding="utf-8")
)["version"]
constant_version = _match(
    r'^INTEGRATION_VERSION\s*=\s*"([^"]+)"',
    (ROOT / "custom_components/gui_recorder/const.py").read_text(encoding="utf-8"),
    "INTEGRATION_VERSION",
)
panel_fallback_version = _match(
    r'_data\.version\s*\|\|\s*"([^"]+)"',
    (ROOT / "custom_components/gui_recorder/frontend/gui-recorder-panel.js").read_text(
        encoding="utf-8"
    ),
    "panel fallback version",
)

versions = {
    "manifest": manifest_version,
    "constant": constant_version,
    "panel fallback": panel_fallback_version,
}
if len(set(versions.values())) != 1:
    raise SystemExit(
        "Version mismatch: " + ", ".join(f"{key}={value}" for key, value in versions.items())
    )

if len(sys.argv) > 1:
    tag_version = sys.argv[1].removeprefix("v")
    if tag_version != manifest_version:
        raise SystemExit(
            f"Release tag {sys.argv[1]} does not match committed version {manifest_version}"
        )

print(manifest_version)

import json
import sys
from pathlib import Path


def render_status(campaign_dir: Path) -> str:
    campaign_dir = Path(campaign_dir)
    manifest_path = campaign_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    frameworks = manifest["frameworks"]
    cases = manifest["cases"]

    lines = []

    header = "  " + "  ".join(cases)
    lines.append(header)

    error_cells = {}

    for fw in frameworks:
        row = fw
        for case in cases:
            cell_dir = campaign_dir / fw / case
            cell_status = _classify_cell(cell_dir)

            if cell_status == "done-ok":
                row += " O"
            elif cell_status == "done-error":
                row += " E"
                error_cells[(fw, case)] = cell_dir
            elif cell_status == "partial":
                row += " …"
            else:  # missing
                row += " ."

        lines.append(row)

    if error_cells:
        lines.append("")
        for (fw, case), cell_dir in error_cells.items():
            meta_path = cell_dir / "meta.json"
            meta = json.loads(meta_path.read_text())
            error_reason = meta.get("error_reason", "unknown")
            lines.append(f"{fw}/{case}: {error_reason}")

    return "\n".join(lines)


def _classify_cell(cell_dir: Path) -> str:
    meta_path = cell_dir / "meta.json"

    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        status = meta.get("status")
        if status == "ok":
            return "done-ok"
        elif status == "error":
            return "done-error"

    if cell_dir.exists():
        return "partial"

    return "missing"


def print_status(campaign_dir: Path, *, file=sys.stdout) -> None:
    output = render_status(campaign_dir)
    print(output, file=file)

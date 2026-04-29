import json
from pathlib import Path

import pytest

from evals.status import render_status, print_status


def test_render_status_missing_cell_appears_as_dot(tmp_path: Path) -> None:
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()

    manifest = {
        "frameworks": ["fw1", "fw2"],
        "cases": ["case1", "case2"],
    }
    (campaign_dir / "manifest.json").write_text(json.dumps(manifest))

    result = render_status(campaign_dir)

    assert "." in result
    assert result.count(".") == 4  # 2 frameworks * 2 cases


def test_render_status_done_ok_cell_appears_as_O(tmp_path: Path) -> None:
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()

    manifest = {
        "frameworks": ["fw1"],
        "cases": ["case1"],
    }
    (campaign_dir / "manifest.json").write_text(json.dumps(manifest))

    cell_dir = campaign_dir / "fw1" / "case1"
    cell_dir.mkdir(parents=True)
    meta = {
        "status": "ok",
    }
    (cell_dir / "meta.json").write_text(json.dumps(meta))

    result = render_status(campaign_dir)

    assert "O" in result


def test_render_status_done_error_cell_appears_as_E(tmp_path: Path) -> None:
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()

    manifest = {
        "frameworks": ["fw1"],
        "cases": ["case1"],
    }
    (campaign_dir / "manifest.json").write_text(json.dumps(manifest))

    cell_dir = campaign_dir / "fw1" / "case1"
    cell_dir.mkdir(parents=True)
    meta = {
        "status": "error",
        "error_reason": "timeout",
    }
    (cell_dir / "meta.json").write_text(json.dumps(meta))

    result = render_status(campaign_dir)

    assert "E" in result
    assert "timeout" in result


def test_render_status_partial_cell_appears_as_ellipsis(tmp_path: Path) -> None:
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()

    manifest = {
        "frameworks": ["fw1"],
        "cases": ["case1"],
    }
    (campaign_dir / "manifest.json").write_text(json.dumps(manifest))

    cell_dir = campaign_dir / "fw1" / "case1"
    cell_dir.mkdir(parents=True)
    (cell_dir / "request.json").write_text("{}")

    result = render_status(campaign_dir)

    assert "…" in result

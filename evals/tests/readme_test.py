from pathlib import Path


def test_readme_documents_pytest_command_normalization() -> None:
    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text()

    assert "pytest command normalization" in text.lower()
    assert "uv run pytest" in text
    assert "python -m pytest" in text

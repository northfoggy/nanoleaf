from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_release_metadata_uses_company_identity():
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'version = "0.2.2"' in metadata
    assert 'authors = [{ name = "Quicksilver Industries LTD." }]' in metadata
    personal_username = "north" + "foggy"
    assert personal_username not in metadata.lower()


def test_pypi_workflow_uses_trusted_publishing():
    workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )

    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert 'python -m pip install ".[dev,release]"' in workflow
    assert "GITHUB_REF_NAME" in workflow
    assert "environment:" in workflow
    assert "name: pypi" in workflow
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "PYPI_API_TOKEN" not in workflow


def test_source_distribution_includes_changelog():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "include CHANGELOG.md" in manifest
    assert "recursive-include deploy *" in manifest

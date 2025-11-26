#!/usr/bin/env python3
"""Tests for the local GitHub mode adapter."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from skill_seekers.cli.github_scraper import GitHubScraper


def _init_temp_repo() -> tuple[str, str]:
    repo_dir = tempfile.mkdtemp(prefix="skill-seekers-local-")
    repo_path = Path(repo_dir)

    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Skill Tests"], cwd=repo_dir, check=True)

    (repo_path / "README.md").write_text("# Local Repo\n", encoding="utf-8")
    (repo_path / "CHANGELOG.md").write_text("## v0.1\n- init\n", encoding="utf-8")
    (repo_path / "sample.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    subprocess.run(["git", "add", "README.md", "CHANGELOG.md", "sample.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo_dir, check=True)

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()
    return repo_dir, head


def _cleanup_output(skill_name: str) -> None:
    output_dir = Path("output")
    data_file = output_dir / f"{skill_name}_github_data.json"
    skill_dir = output_dir / skill_name
    if data_file.exists():
        data_file.unlink()
    if skill_dir.exists():
        shutil.rmtree(skill_dir)


def test_local_github_mode_extracts_metadata():
    repo_dir, head = _init_temp_repo()
    skill_name = "local_mode_test"
    try:
        config = {
            'repo': 'local/test',
            'name': skill_name,
            'github_local_path': repo_dir,
            'include_changelog': True,
            'include_releases': False,
            'include_issues': False,
        }

        scraper = GitHubScraper(config)
        data = scraper.scrape()

        assert data['source_type'] == 'local-github'
        assert data['repo_info']['latest_commit'] == head
        assert data['repo_info']['default_branch'] == 'main'
        assert data['readme'].startswith('# Local Repo')
        assert 'github' in data['source_metadata']['type']
        assert data['source_metadata']['head_commit'] == head
        assert data['changelog'].startswith('## v0.1')

    finally:
        shutil.rmtree(repo_dir)
        _cleanup_output(skill_name)


def test_invalid_local_path_raises_error():
    with tempfile.TemporaryDirectory() as repo_dir:
        config = {
            'repo': 'local/invalid',
            'name': 'invalid_local_repo',
            'github_local_path': repo_dir,
        }

        scraper = GitHubScraper(config)

        with pytest.raises(ValueError, match="目标目录不是有效的 Git 仓库"):
            scraper.scrape()

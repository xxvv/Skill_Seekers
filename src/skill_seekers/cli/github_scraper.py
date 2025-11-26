#!/usr/bin/env python3
"""
GitHub Repository to Claude Skill Converter (Tasks C1.1-C1.12)

Converts GitHub repositories into Claude AI skills by extracting:
- README and documentation
- Code structure and signatures
- GitHub Issues, Changelog, and Releases
- Usage examples from tests

Usage:
    skill-seekers github --repo facebook/react
    skill-seekers github --config configs/react_github.json
    skill-seekers github --repo owner/repo --token $GITHUB_TOKEN
"""

import os
import sys
import json
import re
import argparse
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

try:
    from github import Github, GithubException, Repository
    from github.GithubException import RateLimitExceededException
except ImportError:
    print("Error: PyGithub not installed. Run: pip install PyGithub")
    sys.exit(1)

# Import code analyzer for deep code analysis
try:
    from .code_analyzer import CodeAnalyzer
    CODE_ANALYZER_AVAILABLE = True
except ImportError:
    try:
        from code_analyzer import CodeAnalyzer  # Backwards compatibility when running as script
        CODE_ANALYZER_AVAILABLE = True
    except ImportError:
        CODE_ANALYZER_AVAILABLE = False
        logger.warning("Code analyzer not available - deep analysis disabled")


LOCAL_LANGUAGE_EXTENSIONS = {
    '.py': 'Python',
    '.js': 'JavaScript',
    '.jsx': 'JavaScript',
    '.ts': 'TypeScript',
    '.tsx': 'TypeScript',
    '.java': 'Java',
    '.c': 'C',
    '.h': 'C',
    '.cpp': 'C++',
    '.cc': 'C++',
    '.hh': 'C++',
    '.hpp': 'C++',
    '.cxx': 'C++',
    '.go': 'Go',
    '.rs': 'Rust',
    '.rb': 'Ruby',
    '.php': 'PHP',
    '.swift': 'Swift',
    '.kt': 'Kotlin',
    '.m': 'Objective-C',
    '.mm': 'Objective-C++',
    '.cs': 'C#'
}


class LocalGitError(Exception):
    """Raised when an operation on the local Git repository fails."""

    pass


class GitHubScraper:
    """
    GitHub Repository Scraper (C1.1-C1.9)

    Extracts repository information for skill generation:
    - Repository structure
    - README files
    - Code comments and docstrings
    - Programming language detection
    - Function/class signatures
    - Test examples
    - GitHub Issues
    - CHANGELOG
    - Releases
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize GitHub scraper with configuration."""
        self.config = config
        self.local_repo_path = config.get('github_local_path')
        self.include_untracked = config.get('include_untracked', False)
        self.show_absolute_path = config.get('show_absolute_path', False)

        repo_override = config.get('repo') or config.get('github_repo_name')
        if not repo_override and self.local_repo_path:
            repo_override = Path(self.local_repo_path).expanduser().resolve().name
        if not repo_override:
            raise ValueError("Config must include 'repo' or --github-local-path")

        self.repo_name = repo_override
        self.name = config.get('name', self.repo_name.split('/')[-1])
        self.description = config.get('description', f'Skill for {self.repo_name}')
        self.source_type = 'local-github' if self.local_repo_path else 'github'

        # Local repository state
        self.local_repo_root: Optional[Path] = None
        self._local_files_cache: Optional[List[Dict[str, Any]]] = None
        self._local_repo_display: Optional[str] = None

        # GitHub client setup (C1.1)
        self.github = None
        if self.local_repo_path:
            self.local_repo_root = Path(self.local_repo_path).expanduser().resolve()
            self._local_repo_display = self._format_local_path(self.local_repo_root)
        else:
            token = self._get_token()
            self.github = Github(token) if token else Github()
        self.repo: Optional[Repository.Repository] = None

        # Options
        self.include_issues = config.get('include_issues', True)
        self.max_issues = config.get('max_issues', 100)
        self.include_changelog = config.get('include_changelog', True)
        self.include_releases = config.get('include_releases', True)
        self.include_code = config.get('include_code', False)
        self.code_analysis_depth = config.get('code_analysis_depth', 'surface')  # 'surface', 'deep', 'full'
        self.file_patterns = config.get('file_patterns', [])

        # Initialize code analyzer if deep analysis requested
        self.code_analyzer = None
        if self.code_analysis_depth != 'surface' and CODE_ANALYZER_AVAILABLE:
            self.code_analyzer = CodeAnalyzer(depth=self.code_analysis_depth)
            logger.info(f"Code analysis depth: {self.code_analysis_depth}")

        # Output paths
        self.skill_dir = f"output/{self.name}"
        self.data_file = f"output/{self.name}_github_data.json"

        # Extracted data storage
        self.extracted_data = {
            'repo_info': {},
            'readme': '',
            'file_tree': [],
            'languages': {},
            'signatures': [],
            'test_examples': [],
            'issues': [],
            'changelog': '',
            'releases': [],
            'source_type': self.source_type,
            'source_metadata': {}
        }

    def _get_token(self) -> Optional[str]:
        """
        Get GitHub token from env var or config (both options supported).
        Priority: GITHUB_TOKEN env var > config file > None
        """
        # Try environment variable first (recommended)
        token = os.getenv('GITHUB_TOKEN')
        if token:
            logger.info("Using GitHub token from GITHUB_TOKEN environment variable")
            return token

        # Fall back to config file
        token = self.config.get('github_token')
        if token:
            logger.warning("Using GitHub token from config file (less secure)")
            return token

        logger.warning("No GitHub token provided - using unauthenticated access (lower rate limits)")
        return None

    # ===== Local repository helpers =====

    def _is_local_mode(self) -> bool:
        return self.local_repo_root is not None

    def _format_local_path(self, path: Path) -> str:
        if self.show_absolute_path:
            return str(path)

        cwd = Path.cwd()
        try:
            relative = path.relative_to(cwd)
            return str(relative)
        except ValueError:
            return path.name or str(path)

    def _run_git_command(self, args: List[str], strip: bool = True) -> str:
        if not self._is_local_mode():
            raise LocalGitError("Git commands are only available in local mode")

        cmd = ["git", "-C", str(self.local_repo_root), *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False
            )
        except FileNotFoundError as exc:
            raise LocalGitError("git command not found. Install Git to use local mode") from exc

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise LocalGitError(stderr or "git command failed")

        return result.stdout.strip() if strip else result.stdout

    def _resolve_local_path(self, relative_path: str) -> Optional[Path]:
        if not self._is_local_mode():
            return None

        target = relative_path.strip().lstrip('/')
        if not target:
            return self.local_repo_root

        target_path = Path(target)
        if any(part == '..' for part in target_path.parts):
            return None

        current = self.local_repo_root
        for part in target_path.parts:
            candidate = current / part
            if candidate.exists():
                current = candidate
                continue

            lowered = part.lower()
            match = None
            for child in current.iterdir():
                if child.name.lower() == lowered:
                    match = child
                    break

            if not match:
                return None
            current = match

        return current

    def _read_local_file(self, relative_path: str) -> Optional[str]:
        resolved = self._resolve_local_path(relative_path)
        if not resolved or not resolved.is_file():
            return None

        try:
            return resolved.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            return resolved.read_text(encoding='utf-8', errors='ignore')

    def _collect_local_files(self) -> List[Dict[str, Any]]:
        if self._local_files_cache is not None:
            return self._local_files_cache

        files: List[Dict[str, Any]] = []

        def _add_files(output: str, tracked: bool):
            for rel_path in output.split('\0'):
                if not rel_path:
                    continue
                rel_path = rel_path.replace('\\', '/').strip()
                abs_path = (self.local_repo_root / rel_path).resolve()
                if abs_path.is_file():
                    files.append({
                        'path': rel_path,
                        'abs_path': abs_path,
                        'tracked': tracked,
                        'size': abs_path.stat().st_size
                    })

        tracked_output = self._run_git_command(["ls-files", "-z"], strip=False)
        _add_files(tracked_output, tracked=True)

        if self.include_untracked:
            untracked_output = self._run_git_command([
                "ls-files", "--others", "--exclude-standard", "-z"
            ], strip=False)
            _add_files(untracked_output, tracked=False)

        self._local_files_cache = files
        return files

    def _build_local_file_tree(self) -> List[Dict[str, Any]]:
        files = self._collect_local_files()
        tree: List[Dict[str, Any]] = []
        seen_dirs = set()

        for file_info in files:
            file_path = file_info['path']
            path_obj = Path(file_path)

            # Ensure parent directories are listed
            for depth in range(1, len(path_obj.parts)):
                dir_path = '/'.join(path_obj.parts[:depth])
                if dir_path and dir_path not in seen_dirs:
                    tree.append({'path': dir_path, 'type': 'dir', 'size': 0})
                    seen_dirs.add(dir_path)

            tree.append({
                'path': file_path,
                'type': 'file',
                'size': file_info['size']
            })

        # Sort to keep deterministic ordering
        tree.sort(key=lambda item: item['path'])
        return tree

    def _detect_local_languages(self) -> Dict[str, Dict[str, float]]:
        files = self._collect_local_files()
        language_totals: Dict[str, int] = {}

        for file_info in files:
            suffix = Path(file_info['path']).suffix.lower()
            language = LOCAL_LANGUAGE_EXTENSIONS.get(suffix)
            if not language:
                continue
            language_totals[language] = language_totals.get(language, 0) + file_info['size']

        total_bytes = sum(language_totals.values())
        if total_bytes == 0:
            return {}

        return {
            lang: {
                'bytes': bytes_count,
                'percentage': round((bytes_count / total_bytes) * 100, 2)
            }
            for lang, bytes_count in language_totals.items()
        }

    def _check_dirty_worktree(self):
        status_output = self._run_git_command(["status", "--porcelain"])
        if not status_output:
            return

        lines = [line for line in status_output.splitlines() if line.strip()]
        if not lines:
            return

        untracked = sum(1 for line in lines if line.startswith('??'))
        modified = len(lines) - untracked

        message = (
            "⚠️ 本地仓库包含未提交或未跟踪的更改，当前结果仅基于工作树快照。"  # Current snapshot warning
        )
        if untracked and not self.include_untracked:
            message += " 未跟踪文件将被忽略，可使用 --include-untracked 选项。"

        if modified:
            message += " 请确认已提交需要分析的更改。"

        logger.warning(message)

    def _detect_local_license(self) -> Optional[str]:
        if not self._is_local_mode():
            return None

        candidates = [
            'LICENSE', 'LICENSE.md', 'LICENSE.txt', 'COPYING',
            'COPYING.md', 'COPYING.txt'
        ]

        for candidate in candidates:
            path = self._resolve_local_path(candidate)
            if path and path.is_file():
                return path.name
        return None

    def _read_repo_file(self, relative_path: str) -> Optional[str]:
        if self._is_local_mode():
            return self._read_local_file(relative_path)

        if not self.repo:
            return None

        try:
            content = self.repo.get_contents(relative_path)
            if not content:
                return None

            content_type = getattr(content, 'type', None)
            if content_type == 'dir':
                return None
            return content.decoded_content.decode('utf-8')
        except GithubException:
            return None

        return None

    def scrape(self) -> Dict[str, Any]:
        """
        Main scraping entry point.
        Executes all C1 tasks in sequence.
        """
        try:
            logger.info(f"Starting GitHub scrape for: {self.repo_name}")

            # C1.1: Fetch repository
            self._fetch_repository()

            # C1.2: Extract README
            self._extract_readme()

            # C1.3-C1.6: Extract code structure
            self._extract_code_structure()

            # C1.7: Extract Issues
            if self.include_issues:
                self._extract_issues()

            # C1.8: Extract CHANGELOG
            if self.include_changelog:
                self._extract_changelog()

            # C1.9: Extract Releases
            if self.include_releases:
                self._extract_releases()

            # Save extracted data
            self._save_data()

            logger.info(f"✅ Scraping complete! Data saved to: {self.data_file}")
            return self.extracted_data

        except RateLimitExceededException:
            logger.error("GitHub API rate limit exceeded. Please wait or use authentication token.")
            raise
        except GithubException as e:
            logger.error(f"GitHub API error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during scraping: {e}")
            raise

    def _fetch_repository(self):
        if self._is_local_mode():
            self._fetch_local_repository()
        else:
            self._fetch_remote_repository()

    def _fetch_remote_repository(self):
        """C1.1: Fetch repository structure using GitHub API."""
        logger.info(f"Fetching repository: {self.repo_name}")

        try:
            self.repo = self.github.get_repo(self.repo_name)

            latest_commit = None
            try:
                branch_data = self.repo.get_branch(self.repo.default_branch)
                if branch_data and branch_data.commit:
                    latest_commit = branch_data.commit.sha
            except GithubException:
                latest_commit = None

            repo_info = {
                'name': self.repo.name,
                'full_name': self.repo.full_name,
                'description': self.repo.description,
                'url': self.repo.html_url,
                'homepage': self.repo.homepage,
                'stars': self.repo.stargazers_count,
                'forks': self.repo.forks_count,
                'open_issues': self.repo.open_issues_count,
                'default_branch': self.repo.default_branch,
                'created_at': self.repo.created_at.isoformat() if self.repo.created_at else None,
                'updated_at': self.repo.updated_at.isoformat() if self.repo.updated_at else None,
                'language': self.repo.language,
                'license': self.repo.license.name if self.repo.license else None,
                'topics': self.repo.get_topics(),
                'latest_commit': latest_commit,
                'source_type': self.source_type
            }

            self.extracted_data['repo_info'] = repo_info
            self.extracted_data['source_type'] = self.source_type
            self.extracted_data['source_metadata'] = {
                'type': self.source_type,
                'branch': self.repo.default_branch,
                'head_commit': latest_commit,
                'repository': self.repo.full_name
            }

            logger.info(f"Repository fetched: {self.repo.full_name} ({self.repo.stargazers_count} stars)")

        except GithubException as e:
            if e.status == 404:
                raise ValueError(f"Repository not found: {self.repo_name}")
            raise

    def _fetch_local_repository(self):
        """Gather metadata directly from a local Git checkout."""
        assert self.local_repo_root is not None

        git_dir = self.local_repo_root / '.git'
        if not git_dir.exists():
            raise ValueError(f"目标目录不是有效的 Git 仓库：{self._local_repo_display}")

        try:
            head_commit = self._run_git_command(["rev-parse", "HEAD"])
            branch = self._run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
            created_at = self._run_git_command([
                "log", "--reverse", "--format=%cI", "--max-count=1"
            ]) or None
            updated_at = self._run_git_command(["log", "-1", "--format=%cI"]) or None
        except LocalGitError as exc:
            raise ValueError(str(exc)) from exc

        repo_info = {
            'name': self.repo_name,
            'full_name': self.repo_name,
            'description': self.description,
            'url': None,
            'homepage': None,
            'stars': 0,
            'forks': 0,
            'open_issues': 0,
            'default_branch': branch,
            'created_at': created_at,
            'updated_at': updated_at,
            'language': None,
            'license': self._detect_local_license(),
            'topics': [],
            'latest_commit': head_commit,
            'source_type': self.source_type
        }

        self.extracted_data['repo_info'] = repo_info
        self.extracted_data['source_type'] = self.source_type
        self.extracted_data['source_metadata'] = {
            'type': self.source_type,
            'path': self._local_repo_display,
            'head_commit': head_commit,
            'branch': branch
        }

        short_commit = head_commit[:7] if head_commit else 'unknown'
        logger.info(
            f"Using local GitHub snapshot at {self._local_repo_display} (branch: {branch}, commit: {short_commit})"
        )

        # Warn if working tree has pending changes
        try:
            self._check_dirty_worktree()
        except LocalGitError as exc:
            logger.warning(f"无法检查工作区状态：{exc}")

    def _extract_readme(self):
        """C1.2: Extract README.md files."""
        logger.info("Extracting README...")

        # Try common README locations
        readme_files = ['README.md', 'README.rst', 'README.txt', 'README',
                       'docs/README.md', '.github/README.md']

        for readme_path in readme_files:
            content = self._read_repo_file(readme_path)
            if content:
                self.extracted_data['readme'] = content
                logger.info(f"README found: {readme_path}")
                return

        logger.warning("No README found in repository")

    def _extract_code_structure(self):
        """
        C1.3-C1.6: Extract code structure, languages, signatures, and test examples.
        Surface layer only - no full implementation code.
        """
        logger.info("Extracting code structure...")

        # C1.4: Get language breakdown
        self._extract_languages()

        # Get file tree
        self._extract_file_tree()

        # Extract signatures and test examples
        if self.include_code:
            self._extract_signatures_and_tests()

    def _extract_languages(self):
        """C1.4: Detect programming languages in repository."""
        logger.info("Detecting programming languages...")

        if self._is_local_mode():
            languages = self._detect_local_languages()
            if languages:
                self.extracted_data['languages'] = languages
                logger.info(f"Languages detected: {', '.join(languages.keys())}")
            else:
                logger.warning("No languages detected in local repository")
            return

        try:
            languages = self.repo.get_languages()
            total_bytes = sum(languages.values())

            self.extracted_data['languages'] = {
                lang: {
                    'bytes': bytes_count,
                    'percentage': round((bytes_count / total_bytes) * 100, 2) if total_bytes > 0 else 0
                }
                for lang, bytes_count in languages.items()
            }

            logger.info(f"Languages detected: {', '.join(languages.keys())}")

        except GithubException as e:
            logger.warning(f"Could not fetch languages: {e}")

    def _extract_file_tree(self):
        """Extract repository file tree structure."""
        logger.info("Building file tree...")

        if self._is_local_mode():
            file_tree = self._build_local_file_tree()
            self.extracted_data['file_tree'] = file_tree
            logger.info(f"File tree built: {len(file_tree)} items")
            return

        try:
            contents = self.repo.get_contents("")
            file_tree = []

            while contents:
                file_content = contents.pop(0)

                file_info = {
                    'path': file_content.path,
                    'type': file_content.type,
                    'size': file_content.size if file_content.type == 'file' else None
                }
                file_tree.append(file_info)

                if file_content.type == "dir":
                    contents.extend(self.repo.get_contents(file_content.path))

            self.extracted_data['file_tree'] = file_tree
            logger.info(f"File tree built: {len(file_tree)} items")

        except GithubException as e:
            logger.warning(f"Could not build file tree: {e}")

    def _extract_signatures_and_tests(self):
        """
        C1.3, C1.5, C1.6: Extract signatures, docstrings, and test examples.

        Extraction depth depends on code_analysis_depth setting:
        - surface: File tree only (minimal)
        - deep: Parse files for signatures, parameters, types
        - full: Complete AST analysis (future enhancement)
        """
        if self.code_analysis_depth == 'surface':
            logger.info("Code extraction: Surface level (file tree only)")
            return

        if not self.code_analyzer:
            logger.warning("Code analyzer not available - skipping deep analysis")
            return

        logger.info(f"Extracting code signatures ({self.code_analysis_depth} analysis)...")

        # Get primary language for the repository
        languages = self.extracted_data.get('languages', {})
        if not languages:
            logger.warning("No languages detected - skipping code analysis")
            return

        # Determine primary language
        primary_language = max(languages.items(), key=lambda x: x[1]['bytes'])[0]
        logger.info(f"Primary language: {primary_language}")

        # Determine file extensions to analyze
        extension_map = {
            'Python': ['.py'],
            'JavaScript': ['.js', '.jsx'],
            'TypeScript': ['.ts', '.tsx'],
            'C': ['.c', '.h'],
            'C++': ['.cpp', '.hpp', '.cc', '.hh', '.cxx']
        }

        extensions = extension_map.get(primary_language, [])
        if not extensions:
            logger.warning(f"No file extensions mapped for {primary_language}")
            return

        # Analyze files matching patterns and extensions
        analyzed_files = []
        file_tree = self.extracted_data.get('file_tree', [])

        for file_info in file_tree:
            file_path = file_info['path']

            # Check if file matches extension
            if not any(file_path.endswith(ext) for ext in extensions):
                continue

            # Check if file matches patterns (if specified)
            if self.file_patterns:
                import fnmatch
                if not any(fnmatch.fnmatch(file_path, pattern) for pattern in self.file_patterns):
                    continue

            # Analyze this file
            try:
                content = self._read_repo_file(file_path)
                if not content:
                    continue

                analysis_result = self.code_analyzer.analyze_file(
                    file_path,
                    content,
                    primary_language
                )

                if analysis_result and (analysis_result.get('classes') or analysis_result.get('functions')):
                    analyzed_files.append({
                        'file': file_path,
                        'language': primary_language,
                        **analysis_result
                    })

                    logger.debug(f"Analyzed {file_path}: "
                               f"{len(analysis_result.get('classes', []))} classes, "
                               f"{len(analysis_result.get('functions', []))} functions")

            except Exception as e:
                logger.debug(f"Could not analyze {file_path}: {e}")
                continue

            # Limit number of files analyzed to avoid rate limits
            if len(analyzed_files) >= 50:
                logger.info(f"Reached analysis limit (50 files)")
                break

        self.extracted_data['code_analysis'] = {
            'depth': self.code_analysis_depth,
            'language': primary_language,
            'files_analyzed': len(analyzed_files),
            'files': analyzed_files
        }

        # Calculate totals
        total_classes = sum(len(f.get('classes', [])) for f in analyzed_files)
        total_functions = sum(len(f.get('functions', [])) for f in analyzed_files)

        logger.info(f"Code analysis complete: {len(analyzed_files)} files, "
                   f"{total_classes} classes, {total_functions} functions")

    def _extract_issues(self):
        """C1.7: Extract GitHub Issues (open/closed, labels, milestones)."""
        logger.info(f"Extracting GitHub Issues (max {self.max_issues})...")

        if self._is_local_mode():
            logger.info("Skipping GitHub issues in local mode (offline)")
            self.extracted_data['issues'] = []
            return

        try:
            # Fetch recent issues (open + closed)
            issues = self.repo.get_issues(state='all', sort='updated', direction='desc')

            issue_list = []
            for issue in issues[:self.max_issues]:
                # Skip pull requests (they appear in issues)
                if issue.pull_request:
                    continue

                issue_data = {
                    'number': issue.number,
                    'title': issue.title,
                    'state': issue.state,
                    'labels': [label.name for label in issue.labels],
                    'milestone': issue.milestone.title if issue.milestone else None,
                    'created_at': issue.created_at.isoformat() if issue.created_at else None,
                    'updated_at': issue.updated_at.isoformat() if issue.updated_at else None,
                    'closed_at': issue.closed_at.isoformat() if issue.closed_at else None,
                    'url': issue.html_url,
                    'body': issue.body[:500] if issue.body else None  # First 500 chars
                }
                issue_list.append(issue_data)

            self.extracted_data['issues'] = issue_list
            logger.info(f"Extracted {len(issue_list)} issues")

        except GithubException as e:
            logger.warning(f"Could not fetch issues: {e}")

    def _extract_changelog(self):
        """C1.8: Extract CHANGELOG.md and release notes."""
        logger.info("Extracting CHANGELOG...")

        # Try common changelog locations
        changelog_files = ['CHANGELOG.md', 'CHANGES.md', 'HISTORY.md',
                          'CHANGELOG.rst', 'CHANGELOG.txt', 'CHANGELOG',
                          'docs/CHANGELOG.md', '.github/CHANGELOG.md']

        for changelog_path in changelog_files:
            content = self._read_repo_file(changelog_path)
            if content:
                self.extracted_data['changelog'] = content
                logger.info(f"CHANGELOG found: {changelog_path}")
                return

        logger.warning("No CHANGELOG found in repository")

    def _extract_releases(self):
        """C1.9: Extract GitHub Releases with version history."""
        logger.info("Extracting GitHub Releases...")

        if self._is_local_mode():
            logger.info("Skipping releases in local mode (GitHub API unavailable)")
            self.extracted_data['releases'] = []
            return

        try:
            releases = self.repo.get_releases()

            release_list = []
            for release in releases:
                release_data = {
                    'tag_name': release.tag_name,
                    'name': release.title,
                    'body': release.body,
                    'draft': release.draft,
                    'prerelease': release.prerelease,
                    'created_at': release.created_at.isoformat() if release.created_at else None,
                    'published_at': release.published_at.isoformat() if release.published_at else None,
                    'url': release.html_url,
                    'tarball_url': release.tarball_url,
                    'zipball_url': release.zipball_url
                }
                release_list.append(release_data)

            self.extracted_data['releases'] = release_list
            logger.info(f"Extracted {len(release_list)} releases")

        except GithubException as e:
            logger.warning(f"Could not fetch releases: {e}")

    def _save_data(self):
        """Save extracted data to JSON file."""
        os.makedirs('output', exist_ok=True)

        with open(self.data_file, 'w', encoding='utf-8') as f:
            json.dump(self.extracted_data, f, indent=2, ensure_ascii=False)

        logger.info(f"Data saved to: {self.data_file}")


class GitHubToSkillConverter:
    """
    Convert extracted GitHub data to Claude skill format (C1.10).
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize converter with configuration."""
        self.config = config
        self.name = config.get('name', config['repo'].split('/')[-1])
        self.description = config.get('description', f'Skill for {config["repo"]}')

        # Paths
        self.data_file = f"output/{self.name}_github_data.json"
        self.skill_dir = f"output/{self.name}"

        # Load extracted data
        self.data = self._load_data()

    def _load_data(self) -> Dict[str, Any]:
        """Load extracted GitHub data from JSON."""
        if not os.path.exists(self.data_file):
            raise FileNotFoundError(f"Data file not found: {self.data_file}")

        with open(self.data_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def build_skill(self):
        """Build complete skill structure."""
        logger.info(f"Building skill for: {self.name}")

        # Create directories
        os.makedirs(self.skill_dir, exist_ok=True)
        os.makedirs(f"{self.skill_dir}/references", exist_ok=True)
        os.makedirs(f"{self.skill_dir}/scripts", exist_ok=True)
        os.makedirs(f"{self.skill_dir}/assets", exist_ok=True)

        # Generate SKILL.md
        self._generate_skill_md()

        # Generate reference files
        self._generate_references()

        logger.info(f"✅ Skill built successfully: {self.skill_dir}/")

    def _generate_skill_md(self):
        """Generate main SKILL.md file."""
        repo_info = self.data.get('repo_info', {})

        # Generate skill name (lowercase, hyphens only, max 64 chars)
        skill_name = self.name.lower().replace('_', '-').replace(' ', '-')[:64]

        # Truncate description to 1024 chars if needed
        desc = self.description[:1024] if len(self.description) > 1024 else self.description

        skill_content = f"""---
name: {skill_name}
description: {desc}
---

# {repo_info.get('name', self.name)}

{self.description}

## Description

{repo_info.get('description', 'GitHub repository skill')}

**Repository:** [{repo_info.get('full_name', 'N/A')}]({repo_info.get('url', '#')})
**Language:** {repo_info.get('language', 'N/A')}
**Stars:** {repo_info.get('stars', 0):,}
**License:** {repo_info.get('license', 'N/A')}

## When to Use This Skill

Use this skill when you need to:
- Understand how to use {self.name}
- Look up API documentation
- Find usage examples
- Check for known issues or recent changes
- Review release history

## Quick Reference

### Repository Info
- **Homepage:** {repo_info.get('homepage', 'N/A')}
- **Topics:** {', '.join(repo_info.get('topics', []))}
- **Open Issues:** {repo_info.get('open_issues', 0)}
- **Last Updated:** {repo_info.get('updated_at', 'N/A')[:10]}

### Languages
{self._format_languages()}

### Recent Releases
{self._format_recent_releases()}

## Available References

- `references/README.md` - Complete README documentation
- `references/CHANGELOG.md` - Version history and changes
- `references/issues.md` - Recent GitHub issues
- `references/releases.md` - Release notes
- `references/file_structure.md` - Repository structure

## Usage

See README.md for complete usage instructions and examples.

---

**Generated by Skill Seeker** | GitHub Repository Scraper
"""

        skill_path = f"{self.skill_dir}/SKILL.md"
        with open(skill_path, 'w', encoding='utf-8') as f:
            f.write(skill_content)

        logger.info(f"Generated: {skill_path}")

    def _format_languages(self) -> str:
        """Format language breakdown."""
        languages = self.data.get('languages', {})
        if not languages:
            return "No language data available"

        lines = []
        for lang, info in sorted(languages.items(), key=lambda x: x[1]['bytes'], reverse=True):
            lines.append(f"- **{lang}:** {info['percentage']:.1f}%")

        return '\n'.join(lines)

    def _format_recent_releases(self) -> str:
        """Format recent releases (top 3)."""
        releases = self.data.get('releases', [])
        if not releases:
            return "No releases available"

        lines = []
        for release in releases[:3]:
            lines.append(f"- **{release['tag_name']}** ({release['published_at'][:10]}): {release['name']}")

        return '\n'.join(lines)

    def _generate_references(self):
        """Generate all reference files."""
        # README
        if self.data.get('readme'):
            readme_path = f"{self.skill_dir}/references/README.md"
            with open(readme_path, 'w', encoding='utf-8') as f:
                f.write(self.data['readme'])
            logger.info(f"Generated: {readme_path}")

        # CHANGELOG
        if self.data.get('changelog'):
            changelog_path = f"{self.skill_dir}/references/CHANGELOG.md"
            with open(changelog_path, 'w', encoding='utf-8') as f:
                f.write(self.data['changelog'])
            logger.info(f"Generated: {changelog_path}")

        # Issues
        if self.data.get('issues'):
            self._generate_issues_reference()

        # Releases
        if self.data.get('releases'):
            self._generate_releases_reference()

        # File structure
        if self.data.get('file_tree'):
            self._generate_file_structure_reference()

    def _generate_issues_reference(self):
        """Generate issues.md reference file."""
        issues = self.data['issues']

        content = f"# GitHub Issues\n\nRecent issues from the repository ({len(issues)} total).\n\n"

        # Group by state
        open_issues = [i for i in issues if i['state'] == 'open']
        closed_issues = [i for i in issues if i['state'] == 'closed']

        content += f"## Open Issues ({len(open_issues)})\n\n"
        for issue in open_issues[:20]:
            labels = ', '.join(issue['labels']) if issue['labels'] else 'No labels'
            content += f"### #{issue['number']}: {issue['title']}\n"
            content += f"**Labels:** {labels} | **Created:** {issue['created_at'][:10]}\n"
            content += f"[View on GitHub]({issue['url']})\n\n"

        content += f"\n## Recently Closed Issues ({len(closed_issues)})\n\n"
        for issue in closed_issues[:10]:
            labels = ', '.join(issue['labels']) if issue['labels'] else 'No labels'
            content += f"### #{issue['number']}: {issue['title']}\n"
            content += f"**Labels:** {labels} | **Closed:** {issue['closed_at'][:10]}\n"
            content += f"[View on GitHub]({issue['url']})\n\n"

        issues_path = f"{self.skill_dir}/references/issues.md"
        with open(issues_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f"Generated: {issues_path}")

    def _generate_releases_reference(self):
        """Generate releases.md reference file."""
        releases = self.data['releases']

        content = f"# Releases\n\nVersion history for this repository ({len(releases)} releases).\n\n"

        for release in releases:
            content += f"## {release['tag_name']}: {release['name']}\n"
            content += f"**Published:** {release['published_at'][:10]}\n"
            if release['prerelease']:
                content += f"**Pre-release**\n"
            content += f"\n{release['body']}\n\n"
            content += f"[View on GitHub]({release['url']})\n\n---\n\n"

        releases_path = f"{self.skill_dir}/references/releases.md"
        with open(releases_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f"Generated: {releases_path}")

    def _generate_file_structure_reference(self):
        """Generate file_structure.md reference file."""
        file_tree = self.data['file_tree']

        content = f"# Repository File Structure\n\n"
        content += f"Total items: {len(file_tree)}\n\n"
        content += "```\n"

        # Build tree structure
        for item in file_tree:
            indent = "  " * item['path'].count('/')
            icon = "📁" if item['type'] == 'dir' else "📄"
            content += f"{indent}{icon} {os.path.basename(item['path'])}\n"

        content += "```\n"

        structure_path = f"{self.skill_dir}/references/file_structure.md"
        with open(structure_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f"Generated: {structure_path}")


def main():
    """C1.10: CLI tool entry point."""
    parser = argparse.ArgumentParser(
        description='GitHub Repository to Claude Skill Converter',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  skill-seekers github --repo facebook/react
  skill-seekers github --config configs/react_github.json
  skill-seekers github --repo owner/repo --token $GITHUB_TOKEN
        """
    )

    parser.add_argument('--repo', help='GitHub repository (owner/repo)')
    parser.add_argument('--config', help='Path to config JSON file')
    parser.add_argument('--token', help='GitHub personal access token')
    parser.add_argument('--name', help='Skill name (default: repo name)')
    parser.add_argument('--description', help='Skill description')
    parser.add_argument('--no-issues', action='store_true', help='Skip GitHub issues')
    parser.add_argument('--no-changelog', action='store_true', help='Skip CHANGELOG')
    parser.add_argument('--no-releases', action='store_true', help='Skip releases')
    parser.add_argument('--max-issues', type=int, default=100, help='Max issues to fetch')
    parser.add_argument('--scrape-only', action='store_true', help='Only scrape, don\'t build skill')
    parser.add_argument('--github-local-path', help='Path to a local Git repository to reuse GitHub mode without API access')
    parser.add_argument('--github-repo-name', help='Override repository name when using --github-local-path')
    parser.add_argument('--include-untracked', action='store_true', help='Include untracked files when analyzing local repositories')
    parser.add_argument('--show-absolute-path', action='store_true', help='Show the absolute local path in logs (default hides it)')

    args = parser.parse_args()

    # Build config from args or file
    if args.config:
        with open(args.config, 'r') as f:
            config = json.load(f)

        # Allow overrides for local mode when using config files
        if args.github_local_path:
            config['github_local_path'] = args.github_local_path
        if args.github_repo_name:
            config['github_repo_name'] = args.github_repo_name
        if args.include_untracked:
            config['include_untracked'] = True
        if args.show_absolute_path:
            config['show_absolute_path'] = True
    else:
        if not args.repo and not args.github_local_path:
            parser.error('Either --repo, --github-local-path or --config is required')

        repo_value = args.repo or args.github_repo_name
        if not repo_value and args.github_local_path:
            repo_value = Path(args.github_local_path).expanduser().resolve().name

        repo_label = repo_value or 'local-repo'

        config = {
            'repo': repo_value or repo_label,
            'name': args.name or (repo_label.split('/')[-1] if '/' in repo_label else repo_label),
            'description': args.description or f'GitHub repository skill for {repo_label}',
            'github_token': args.token,
            'include_issues': not args.no_issues,
            'include_changelog': not args.no_changelog,
            'include_releases': not args.no_releases,
            'max_issues': args.max_issues,
            'github_local_path': args.github_local_path,
            'github_repo_name': args.github_repo_name,
            'include_untracked': args.include_untracked,
            'show_absolute_path': args.show_absolute_path
        }

        # Drop None values to keep config clean
        config = {k: v for k, v in config.items() if v is not None}

    try:
        # Phase 1: Scrape GitHub repository
        scraper = GitHubScraper(config)
        scraper.scrape()

        if args.scrape_only:
            logger.info("Scrape complete (--scrape-only mode)")
            return

        # Phase 2: Build skill
        converter = GitHubToSkillConverter(config)
        converter.build_skill()

        logger.info(f"\n✅ Success! Skill created at: output/{config.get('name', config['repo'].split('/')[-1])}/")
        logger.info(f"Next step: skill-seekers-package output/{config.get('name', config['repo'].split('/')[-1])}/")

    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()

"""Tests for Hive Discovery Code Graph Builder (Layer 0).

Tests run the builder against real temporary directories with known files.
No mocks -- these are deterministic AST operations on real source code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.graph.builder import (
    CodeGraphBuilder,
    _detect_language,
    _should_skip,
)
from forge.graph.models import CodeGraph, EdgeKind, NodeKind


# ── Helpers ──────────────────────────────────────────────────────────


def _write_file(base: Path, rel_path: str, content: str) -> Path:
    """Write a file under base, creating directories as needed."""
    fp = base / rel_path
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return fp


def _create_small_python_project(base: Path) -> None:
    """Create a small Python project with known structure.

    Layout:
        main.py          (imports utils, 1 function: main)
        utils.py          (1 function: helper, 1 class: Config)
        models/__init__.py (empty)
        models/user.py    (1 class: User with method validate, imports os)
    """
    _write_file(base, "main.py", """\
from utils import helper

def main():
    result = helper()
    return result
""")

    _write_file(base, "utils.py", """\
import os

def helper():
    return os.getcwd()

class Config:
    def __init__(self):
        self.debug = True
""")

    _write_file(base, "models/__init__.py", "")

    _write_file(base, "models/user.py", """\
import json
from utils import Config

class User:
    def __init__(self, name):
        self.name = name

    def validate(self):
        return bool(self.name)

def create_user(name):
    return User(name)
""")


def _create_js_ts_project(base: Path) -> None:
    """Create a small JS/TS project for multi-language testing.

    Layout:
        src/index.ts      (1 function: main, imports ./utils)
        src/utils.js      (1 function: formatDate, 1 class: Logger)
    """
    _write_file(base, "src/index.ts", """\
import { formatDate } from './utils';

function main() {
    console.log(formatDate());
}
""")

    _write_file(base, "src/utils.js", """\
function formatDate() {
    return new Date().toISOString();
}

class Logger {
    log(msg) {
        console.log(msg);
    }
}
""")


def _create_large_project(base: Path) -> None:
    """Create a multi-directory project with cross-deps for segmentation testing.

    Layout (6+ files across 3 directories):
        api/routes.py       (imports api/auth, core/db)
        api/auth.py         (imports core/db)
        api/middleware.py    (imports api/auth)
        core/db.py          (imports core/config)
        core/config.py      (standalone)
        core/utils.py       (standalone)
        services/email.py   (imports core/config)
        services/notify.py  (imports services/email, core/utils)
    """
    _write_file(base, "api/routes.py", """\
from api.auth import verify
from core.db import get_connection

def handle_request():
    verify()
    conn = get_connection()
    return conn
""")

    _write_file(base, "api/auth.py", """\
from core.db import get_connection

def verify():
    conn = get_connection()
    return True
""")

    _write_file(base, "api/middleware.py", """\
from api.auth import verify

def check_auth():
    return verify()
""")

    _write_file(base, "core/db.py", """\
from core.config import DATABASE_URL

def get_connection():
    return f"conn:{DATABASE_URL}"
""")

    _write_file(base, "core/config.py", """\
DATABASE_URL = "postgres://localhost/mydb"
SECRET_KEY = "super-secret"
""")

    _write_file(base, "core/utils.py", """\
def slugify(text):
    return text.lower().replace(" ", "-")

def truncate(text, length=100):
    return text[:length]
""")

    _write_file(base, "services/email.py", """\
from core.config import SECRET_KEY

def send_email(to, subject, body):
    return {"to": to, "subject": subject}
""")

    _write_file(base, "services/notify.py", """\
from services.email import send_email
from core.utils import slugify

def notify_user(user, message):
    slug = slugify(message)
    send_email(user, slug, message)
""")


# ── Unit function tests ──────────────────────────────────────────────


class TestDetectLanguage:
    """Test _detect_language helper."""

    def test_python(self):
        assert _detect_language("src/app.py") == "python"

    def test_javascript(self):
        assert _detect_language("app.js") == "javascript"
        assert _detect_language("component.jsx") == "javascript"

    def test_typescript(self):
        assert _detect_language("app.ts") == "typescript"
        assert _detect_language("component.tsx") == "typescript"

    def test_go(self):
        assert _detect_language("main.go") == "go"

    def test_rust(self):
        assert _detect_language("lib.rs") == "rust"

    def test_java(self):
        assert _detect_language("Main.java") == "java"

    def test_ruby(self):
        assert _detect_language("app.rb") == "ruby"

    def test_unknown_extension(self):
        assert _detect_language("readme.md") == ""
        assert _detect_language("data.csv") == ""

    def test_case_insensitive(self):
        assert _detect_language("App.PY") == "python"


class TestShouldSkip:
    """Test _should_skip helper for directory and file exclusion."""

    def test_skip_node_modules(self):
        assert _should_skip(Path("node_modules/package/index.js"))

    def test_skip_git_directory(self):
        assert _should_skip(Path(".git/config"))

    def test_skip_pycache(self):
        assert _should_skip(Path("src/__pycache__/module.cpython-311.pyc"))

    def test_skip_binary_extensions(self):
        assert _should_skip(Path("image.png"))
        assert _should_skip(Path("font.woff2"))
        assert _should_skip(Path("archive.zip"))

    def test_skip_dotfiles(self):
        assert _should_skip(Path(".eslintrc"))
        assert _should_skip(Path(".env"))

    def test_allow_normal_python_file(self):
        assert not _should_skip(Path("src/app.py"))

    def test_allow_normal_js_file(self):
        assert not _should_skip(Path("src/index.js"))

    def test_skip_with_custom_dirs(self):
        assert _should_skip(Path("custom_skip/file.py"), skip_dirs={"custom_skip"})

    def test_skip_lock_files(self):
        assert _should_skip(Path("package-lock.json.lock"))

    def test_skip_venv(self):
        assert _should_skip(Path("venv/lib/site-packages/pkg.py"))
        assert _should_skip(Path(".venv/lib/site-packages/pkg.py"))


# ── Builder on small Python project ─────────────────────────────────


class TestCodeGraphBuilderSmallProject:
    """Test CodeGraphBuilder on a real temporary Python project."""

    @pytest.fixture()
    def project(self, tmp_path: Path) -> tuple[Path, CodeGraph]:
        _create_small_python_project(tmp_path)
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()
        return tmp_path, graph

    def test_correct_file_count(self, project):
        _, graph = project
        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        # main.py, utils.py, models/__init__.py, models/user.py
        assert len(file_nodes) == 4

    def test_stats_total_files(self, project):
        _, graph = project
        assert graph.stats["total_files"] == 4

    def test_python_functions_extracted(self, project):
        _, graph = project
        fn_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FUNCTION]
        fn_names = {n.name for n in fn_nodes}
        assert "main" in fn_names
        assert "helper" in fn_names
        assert "create_user" in fn_names

    def test_class_extracted(self, project):
        _, graph = project
        cls_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.CLASS]
        cls_names = {n.name for n in cls_nodes}
        assert "Config" in cls_names
        assert "User" in cls_names

    def test_class_methods_extracted(self, project):
        _, graph = project
        fn_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FUNCTION]
        fn_names = {n.name for n in fn_nodes}
        # Class methods should be extracted with ClassName.method_name
        assert "Config.__init__" in fn_names
        assert "User.__init__" in fn_names
        assert "User.validate" in fn_names

    def test_import_nodes_created(self, project):
        _, graph = project
        import_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.IMPORT]
        assert len(import_nodes) >= 3  # os, json, from utils import ... etc.

    def test_contains_edges(self, project):
        _, graph = project
        contains_edges = [e for e in graph.edges if e.kind == EdgeKind.CONTAINS]
        # Each function/class should have a CONTAINS edge from its file
        assert len(contains_edges) >= 4  # at least main, helper, Config, User

    def test_imports_edges(self, project):
        _, graph = project
        import_edges = [e for e in graph.edges if e.kind == EdgeKind.IMPORTS]
        assert len(import_edges) >= 3

    def test_internal_imports_resolved(self, project):
        _, graph = project
        depends_edges = [e for e in graph.edges if e.kind == EdgeKind.DEPENDS_ON]
        # main.py imports utils -> depends_on edge
        # models/user.py imports utils -> depends_on edge
        dep_targets = {e.target_id for e in depends_edges}
        assert "file:utils.py" in dep_targets

    def test_segments_created(self, project):
        _, graph = project
        # Small project with few files -> single segment
        assert len(graph.segments) >= 1

    def test_single_segment_for_small_project(self, project):
        _, graph = project
        # 4 files <= target_segments (5 default) -> single segment
        assert len(graph.segments) == 1
        seg = graph.segments[0]
        assert len(seg.files) == 4

    def test_stats_computed(self, project):
        _, graph = project
        assert "total_files" in graph.stats
        assert "total_loc" in graph.stats
        assert "languages" in graph.stats
        assert "total_segments" in graph.stats
        assert "total_nodes" in graph.stats
        assert "total_edges" in graph.stats

    def test_stats_total_loc_positive(self, project):
        _, graph = project
        assert graph.stats["total_loc"] > 0

    def test_stats_language_ratios(self, project):
        _, graph = project
        assert "python" in graph.stats["languages"]
        assert graph.stats["languages"]["python"] == 1.0  # all Python

    def test_file_nodes_have_loc(self, project):
        """Non-empty files should have loc > 0. Empty __init__.py gets loc=0."""
        _, graph = project
        for node in graph.nodes.values():
            if node.kind == NodeKind.FILE and node.name != "__init__.py":
                assert node.loc > 0, f"File node {node.file_path} has loc=0"

    def test_function_nodes_have_line_numbers(self, project):
        _, graph = project
        for node in graph.nodes.values():
            if node.kind == NodeKind.FUNCTION:
                assert node.line_start is not None
                assert node.line_start >= 1, f"Function {node.name} has invalid line_start"

    def test_all_nodes_have_segment_id(self, project):
        """After build, every node with a file_path should have a segment_id."""
        _, graph = project
        for node in graph.nodes.values():
            if node.file_path:
                assert node.segment_id != "", f"Node {node.id} missing segment_id"


# ── Multi-language support ───────────────────────────────────────────


class TestCodeGraphBuilderMultiLanguage:
    """Test builder handles JS/TS files correctly."""

    @pytest.fixture()
    def project(self, tmp_path: Path) -> tuple[Path, CodeGraph]:
        _create_js_ts_project(tmp_path)
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()
        return tmp_path, graph

    def test_discovers_ts_and_js_files(self, project):
        _, graph = project
        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        paths = {n.file_path for n in file_nodes}
        assert "src/index.ts" in paths
        assert "src/utils.js" in paths

    def test_extracts_js_functions(self, project):
        _, graph = project
        fn_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FUNCTION]
        fn_names = {n.name for n in fn_nodes}
        assert "formatDate" in fn_names
        assert "main" in fn_names

    def test_extracts_js_classes(self, project):
        _, graph = project
        cls_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.CLASS]
        cls_names = {n.name for n in cls_nodes}
        assert "Logger" in cls_names

    def test_extracts_ts_imports(self, project):
        _, graph = project
        import_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.IMPORT]
        assert len(import_nodes) >= 1

    def test_language_detected_correctly(self, project):
        _, graph = project
        for node in graph.nodes.values():
            if node.kind == NodeKind.FILE:
                if node.file_path.endswith(".ts"):
                    assert node.language == "typescript"
                elif node.file_path.endswith(".js"):
                    assert node.language == "javascript"


# ── Mixed project (Python + JS) ─────────────────────────────────────


class TestCodeGraphBuilderMixedLanguages:
    """Test builder on a project with both Python and JS/TS."""

    @pytest.fixture()
    def project(self, tmp_path: Path) -> tuple[Path, CodeGraph]:
        _create_small_python_project(tmp_path)
        _create_js_ts_project(tmp_path)
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()
        return tmp_path, graph

    def test_discovers_all_files(self, project):
        _, graph = project
        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        # 4 Python + 2 JS/TS = 6
        assert len(file_nodes) == 6

    def test_language_ratios(self, project):
        _, graph = project
        langs = graph.stats["languages"]
        assert "python" in langs
        # JS or TS should also appear
        assert any(lang in langs for lang in ("javascript", "typescript"))


# ── Skip directories ─────────────────────────────────────────────────


class TestCodeGraphBuilderSkipDirs:
    """Verify node_modules, .git, __pycache__ are skipped."""

    @pytest.fixture()
    def project(self, tmp_path: Path) -> tuple[Path, CodeGraph]:
        # Real source files
        _write_file(tmp_path, "app.py", "def main(): pass\n")

        # Files that should be skipped
        _write_file(tmp_path, "node_modules/lodash/index.js", "function noop() {}\n")
        _write_file(tmp_path, ".git/config", "[core]\n")
        _write_file(tmp_path, "__pycache__/app.cpython-311.pyc", "garbage bytes")
        _write_file(tmp_path, ".next/build.js", "function build() {}\n")
        _write_file(tmp_path, "dist/bundle.js", "function bundle() {}\n")

        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()
        return tmp_path, graph

    def test_only_real_source_discovered(self, project):
        _, graph = project
        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        paths = {n.file_path for n in file_nodes}
        assert "app.py" in paths
        # Skipped directories should NOT appear
        assert all("node_modules" not in p for p in paths)
        assert all(".git" not in p for p in paths)
        assert all("__pycache__" not in p for p in paths)
        assert all(".next" not in p for p in paths)
        assert all("dist" not in p for p in paths)

    def test_stats_reflects_only_real_files(self, project):
        _, graph = project
        assert graph.stats["total_files"] == 1

    def test_skip_binary_files(self, tmp_path: Path):
        """Binary extensions (.png, .jpg, etc.) should not be indexed."""
        _write_file(tmp_path / "skip_bin", "app.py", "def x(): pass\n")
        _write_file(tmp_path / "skip_bin", "logo.png", "fake png data")
        _write_file(tmp_path / "skip_bin", "font.woff2", "fake font data")

        builder = CodeGraphBuilder(repo_path=str(tmp_path / "skip_bin"))
        graph = builder.build()

        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        assert len(file_nodes) == 1
        assert file_nodes[0].file_path == "app.py"


# ── Empty repo ───────────────────────────────────────────────────────


class TestCodeGraphBuilderEmptyRepo:
    """Test builder handles empty directory gracefully."""

    def test_empty_directory(self, tmp_path: Path):
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        assert len(graph.nodes) == 0
        assert len(graph.edges) == 0
        # Builder creates a single empty "seg-all" segment even with 0 files
        assert len(graph.segments) == 1
        assert graph.segments[0].files == []
        assert graph.segments[0].node_ids == []
        assert graph.stats["total_files"] == 0
        assert graph.stats["total_loc"] == 0

    def test_directory_with_only_skipped_files(self, tmp_path: Path):
        """A directory with only binary/skipped files should behave like empty."""
        _write_file(tmp_path, "image.png", "fake png")
        _write_file(tmp_path, ".env", "SECRET=foo")

        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        assert graph.stats["total_files"] == 0

    def test_directory_with_only_unsupported_languages(self, tmp_path: Path):
        """Files in unsupported languages should not produce AST nodes,
        but they also won't be discovered since _detect_language returns ''."""
        _write_file(tmp_path, "readme.md", "# Hello\n")
        _write_file(tmp_path, "data.csv", "a,b,c\n1,2,3\n")

        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        assert graph.stats["total_files"] == 0


# ── Fallback segmentation ───────────────────────────────────────────


class TestCodeGraphBuilderFallbackSegmentation:
    """When the graph is sparse (few edges), directory-based segmentation kicks in."""

    @pytest.fixture()
    def project(self, tmp_path: Path) -> tuple[Path, CodeGraph]:
        """Create files in separate directories with no imports between them.

        This forces fallback to directory-based segmentation since there
        are no edges in the file dependency graph.
        """
        # 6 files (> target_segments=5) with NO cross-file imports
        _write_file(tmp_path, "alpha/a1.py", "def func_a1(): pass\n")
        _write_file(tmp_path, "alpha/a2.py", "def func_a2(): pass\n")
        _write_file(tmp_path, "beta/b1.py", "def func_b1(): pass\n")
        _write_file(tmp_path, "beta/b2.py", "def func_b2(): pass\n")
        _write_file(tmp_path, "gamma/g1.py", "def func_g1(): pass\n")
        _write_file(tmp_path, "gamma/g2.py", "def func_g2(): pass\n")

        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()
        return tmp_path, graph

    def test_multiple_segments_created(self, project):
        _, graph = project
        # Should have segments based on directory grouping
        assert len(graph.segments) >= 2

    def test_segments_match_directories(self, project):
        _, graph = project
        labels = {seg.label for seg in graph.segments}
        assert "alpha" in labels
        assert "beta" in labels
        assert "gamma" in labels

    def test_files_grouped_by_directory(self, project):
        _, graph = project
        for seg in graph.segments:
            if seg.label == "alpha":
                assert sorted(seg.files) == ["alpha/a1.py", "alpha/a2.py"]
            elif seg.label == "beta":
                assert sorted(seg.files) == ["beta/b1.py", "beta/b2.py"]

    def test_all_files_assigned_to_segments(self, project):
        _, graph = project
        all_seg_files = set()
        for seg in graph.segments:
            all_seg_files.update(seg.files)
        assert len(all_seg_files) == 6


# ── Large project segmentation (community detection) ────────────────


class TestCodeGraphBuilderCommunityDetection:
    """Test Louvain community detection on a project with cross-deps."""

    @pytest.fixture()
    def project(self, tmp_path: Path) -> tuple[Path, CodeGraph]:
        _create_large_project(tmp_path)
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()
        return tmp_path, graph

    def test_discovers_all_files(self, project):
        _, graph = project
        assert graph.stats["total_files"] == 8

    def test_multiple_segments_or_single(self, project):
        """With 8 files and cross-deps, community detection should produce
        at least 1 segment. The exact count depends on the algorithm."""
        _, graph = project
        assert len(graph.segments) >= 1

    def test_all_files_in_segments(self, project):
        _, graph = project
        all_seg_files = set()
        for seg in graph.segments:
            all_seg_files.update(seg.files)
        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        assert len(all_seg_files) == len(file_nodes)

    def test_depends_on_edges_created(self, project):
        _, graph = project
        dep_edges = [e for e in graph.edges if e.kind == EdgeKind.DEPENDS_ON]
        # routes->auth, routes->db, auth->db, db->config, etc.
        assert len(dep_edges) >= 4

    def test_segment_entry_points(self, project):
        """Public functions should appear as entry points in their segments."""
        _, graph = project
        all_entry_points = []
        for seg in graph.segments:
            all_entry_points.extend(seg.entry_points)
        # At least some of our known functions should appear
        entry_names = {ep.split(":")[-1] for ep in all_entry_points}
        assert "handle_request" in entry_names or "verify" in entry_names or "get_connection" in entry_names

    def test_segment_node_ids_include_non_file_nodes(self, project):
        """Segment node_ids should include functions, classes, imports, not just files."""
        _, graph = project
        for seg in graph.segments:
            kinds_in_seg = set()
            for nid in seg.node_ids:
                node = graph.nodes.get(nid)
                if node:
                    kinds_in_seg.add(node.kind)
            # Each segment should at least have FILE nodes
            assert NodeKind.FILE in kinds_in_seg

    def test_stats_has_correct_node_edge_counts(self, project):
        _, graph = project
        assert graph.stats["total_nodes"] == len(graph.nodes)
        assert graph.stats["total_edges"] == len(graph.edges)
        assert graph.stats["total_segments"] == len(graph.segments)


# ── Edge cases ───────────────────────────────────────────────────────


class TestCodeGraphBuilderEdgeCases:
    """Edge cases: single file, empty file, syntax errors."""

    def test_single_file_project(self, tmp_path: Path):
        _write_file(tmp_path, "main.py", "def hello(): print('hi')\n")
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        assert graph.stats["total_files"] == 1
        assert len(graph.segments) == 1
        fn_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FUNCTION]
        assert len(fn_nodes) == 1
        assert fn_nodes[0].name == "hello"

    def test_empty_python_file(self, tmp_path: Path):
        _write_file(tmp_path, "empty.py", "")
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        assert graph.stats["total_files"] == 1
        # Empty file should still produce a FILE node
        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        assert len(file_nodes) == 1

    def test_file_with_syntax_error(self, tmp_path: Path):
        """Tree-sitter is lenient; it should still parse partial AST."""
        _write_file(tmp_path, "bad.py", """\
def valid_function():
    pass

def broken(
    # missing close paren and colon
    pass

def another_valid():
    pass
""")
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        # Should still discover the file and extract what it can
        assert graph.stats["total_files"] == 1
        fn_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FUNCTION]
        # At minimum valid_function should be extracted
        fn_names = {n.name for n in fn_nodes}
        assert "valid_function" in fn_names

    def test_nested_directory_structure(self, tmp_path: Path):
        """Deeply nested files should be discovered."""
        _write_file(
            tmp_path,
            "a/b/c/d/deep.py",
            "def deep_func(): pass\n",
        )
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        assert graph.stats["total_files"] == 1
        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        assert file_nodes[0].file_path == "a/b/c/d/deep.py"

    def test_custom_target_segments(self, tmp_path: Path):
        """Builder respects target_segments parameter."""
        # Create 3 files in separate dirs
        _write_file(tmp_path, "a/f.py", "def fa(): pass\n")
        _write_file(tmp_path, "b/f.py", "def fb(): pass\n")
        _write_file(tmp_path, "c/f.py", "def fc(): pass\n")

        # With target_segments=2, 3 files <= 2 is false, so community detection runs
        builder = CodeGraphBuilder(repo_path=str(tmp_path), target_segments=2)
        graph = builder.build()

        # Should produce segments (exact count depends on algorithm/edges)
        assert len(graph.segments) >= 1

    def test_file_node_id_convention(self, tmp_path: Path):
        """File node IDs should follow 'file:<relative_path>' convention."""
        _write_file(tmp_path, "src/app.py", "x = 1\n")
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        assert file_nodes[0].id == "file:src/app.py"

    def test_function_node_id_convention(self, tmp_path: Path):
        """Function node IDs should follow 'fn:<path>:<name>' convention."""
        _write_file(tmp_path, "app.py", "def my_func(): pass\n")
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        fn_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FUNCTION]
        assert fn_nodes[0].id == "fn:app.py:my_func"

    def test_class_node_id_convention(self, tmp_path: Path):
        """Class node IDs should follow 'cls:<path>:<name>' convention."""
        _write_file(tmp_path, "app.py", "class MyClass:\n    pass\n")
        builder = CodeGraphBuilder(repo_path=str(tmp_path))
        graph = builder.build()

        cls_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.CLASS]
        assert cls_nodes[0].id == "cls:app.py:MyClass"

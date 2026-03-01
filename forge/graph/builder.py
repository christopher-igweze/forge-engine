"""Code Graph Builder — Layer 0 of Hive Discovery.

Deterministic AST-based code analysis. No LLM involved.

Pipeline:
  1. Tree-sitter AST parsing (per-file) → nodes + edges
  2. Dependency graph extraction (package manifests + imports)
  3. Community detection (modularity-based) → segments
  4. Output: CodeGraph ready for Layer 1 swarm workers

Falls back to directory-based segmentation when AST parsing fails.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import networkx as nx

from forge.graph.models import (
    CodeGraph,
    EdgeKind,
    GraphEdge,
    GraphNode,
    NodeKind,
    Segment,
)

# Re-export extracted functions for backward compatibility.
from forge.graph.language_extractors import (  # noqa: F401
    _detect_language,
    _extract_go,
    _extract_java,
    _extract_js_ts,
    _extract_python,
    _extract_ruby,
    _extract_rust,
    _parse_file_ast,
)
from forge.graph.segmentation import (  # noqa: F401
    _communities_to_segments,
    _segment_by_community_detection,
    _segment_by_directory,
)

logger = logging.getLogger(__name__)

# ── Language registration ────────────────────────────────────────────

_LANG_REGISTRY: dict[str, Any] = {}

_LANG_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
}

_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".next", ".nuxt",
    "dist", "build", ".cache", "coverage", ".venv", "venv",
    ".artifacts", ".local-test", ".forge-worktrees", ".tox",
    "target", "vendor", ".mypy_cache", ".pytest_cache",
}

_SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".wav",
    ".zip", ".tar", ".gz", ".br", ".pyc", ".pyo", ".so", ".dll",
    ".lock", ".map", ".min.js", ".min.css",
}


def _get_language(lang_name: str):
    """Lazy-load a tree-sitter language."""
    if lang_name in _LANG_REGISTRY:
        return _LANG_REGISTRY[lang_name]

    try:
        import tree_sitter as ts

        lang_module = None
        if lang_name == "python":
            import tree_sitter_python as lang_module
        elif lang_name == "javascript":
            import tree_sitter_javascript as lang_module
        elif lang_name == "typescript":
            import tree_sitter_typescript as lang_module
        elif lang_name == "go":
            import tree_sitter_go as lang_module
        elif lang_name == "rust":
            import tree_sitter_rust as lang_module
        elif lang_name == "java":
            import tree_sitter_java as lang_module
        elif lang_name == "ruby":
            import tree_sitter_ruby as lang_module

        if lang_module is not None:
            if lang_name == "typescript":
                lang = ts.Language(lang_module.language_typescript())
            else:
                lang = ts.Language(lang_module.language())
            _LANG_REGISTRY[lang_name] = lang
            return lang
    except (ImportError, Exception) as e:
        logger.debug("Tree-sitter language %s not available: %s", lang_name, e)

    _LANG_REGISTRY[lang_name] = None
    return None


def _should_skip(path: Path, skip_dirs: set[str] | None = None) -> bool:
    """Check if a file or directory should be skipped."""
    dirs = skip_dirs or _SKIP_DIRS
    if any(d in path.parts for d in dirs):
        return True
    if path.suffix.lower() in _SKIP_EXTENSIONS:
        return True
    if path.name.startswith("."):
        return True
    return False


# ── Import resolution (internal dependencies) ───────────────────────


def _resolve_internal_imports(
    file_path: str,
    import_nodes: list[GraphNode],
    all_file_paths: set[str],
    root_path: str,
) -> list[str]:
    """Resolve import nodes to internal file paths.

    Returns list of file paths that this file imports from the project.
    """
    resolved = []
    root = Path(root_path)

    for node in import_nodes:
        module = node.metadata.get("module", node.name)
        if not module:
            continue

        # Python: convert dotted module to path
        candidates = []
        module_path = module.replace(".", "/")
        candidates.append(f"{module_path}.py")
        candidates.append(f"{module_path}/__init__.py")

        # JS/TS: relative imports
        if module.startswith("."):
            dir_of_file = str(Path(file_path).parent)
            rel = os.path.normpath(os.path.join(dir_of_file, module))
            for ext in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                candidates.append(rel + ext)

        # Absolute imports (JS/TS with src/ prefix)
        for ext in ("", ".ts", ".tsx", ".js", ".jsx"):
            candidates.append(f"src/{module_path}{ext}")

        for candidate in candidates:
            normalized = os.path.normpath(candidate)
            if normalized in all_file_paths:
                resolved.append(normalized)
                break

    return resolved


# ── Main Builder ─────────────────────────────────────────────────────


class CodeGraphBuilder:
    """Build a CodeGraph from a repository path.

    Usage:
        builder = CodeGraphBuilder(repo_path="/path/to/repo")
        graph = builder.build()
    """

    def __init__(
        self,
        repo_path: str,
        target_segments: int = 5,
        min_segment_size: int = 2,
    ):
        self.repo_path = repo_path
        self.target_segments = target_segments
        self.min_segment_size = min_segment_size
        self._graph = CodeGraph()
        self._nx_graph = nx.Graph()
        self._all_file_paths: set[str] = set()

    def build(self) -> CodeGraph:
        """Execute the full build pipeline.

        1. Discover and index all source files
        2. Parse ASTs and extract nodes + edges
        3. Resolve internal imports to file dependencies
        4. Segment via community detection
        5. Compute segment inter-dependencies
        """
        logger.info("CodeGraphBuilder: starting for %s", self.repo_path)

        # Step 1: Discover files
        self._discover_files()
        logger.info("CodeGraphBuilder: discovered %d files", len(self._all_file_paths))

        # Step 2: Parse ASTs
        self._parse_all_files()
        logger.info(
            "CodeGraphBuilder: parsed %d nodes, %d edges",
            len(self._graph.nodes), len(self._graph.edges),
        )

        # Step 3: Resolve internal imports
        self._resolve_imports()

        # Step 4: Segment
        segments = _segment_by_community_detection(
            self._graph,
            self._nx_graph,
            target_segments=self.target_segments,
            min_segment_size=self.min_segment_size,
        )
        self._graph.segments = segments

        # Step 5: Assign segment IDs to nodes and compute inter-dependencies
        self._assign_segments_and_deps()

        # Compute stats
        lang_counts: dict[str, int] = defaultdict(int)
        for n in self._graph.nodes.values():
            if n.kind == NodeKind.FILE and n.language:
                lang_counts[n.language] += n.loc

        total_loc = sum(lang_counts.values())
        lang_ratios = {
            lang: round(loc / total_loc, 2) if total_loc > 0 else 0
            for lang, loc in sorted(lang_counts.items(), key=lambda x: -x[1])
        }

        self._graph.stats = {
            "total_files": len(self._all_file_paths),
            "total_loc": total_loc,
            "languages": lang_ratios,
            "total_segments": len(segments),
            "total_nodes": len(self._graph.nodes),
            "total_edges": len(self._graph.edges),
        }

        logger.info(
            "CodeGraphBuilder: complete — %d files, %d segments, %d LOC",
            len(self._all_file_paths), len(segments), total_loc,
        )

        return self._graph

    def _discover_files(self) -> None:
        """Walk the repo and discover all source files."""
        root = Path(self.repo_path)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]

            for fname in filenames:
                fpath = Path(dirpath) / fname
                if _should_skip(fpath):
                    continue

                lang = _detect_language(str(fpath))
                if not lang:
                    continue

                rel_path = str(fpath.relative_to(root))
                self._all_file_paths.add(rel_path)

    def _parse_all_files(self) -> None:
        """Parse all discovered files with tree-sitter."""
        root = Path(self.repo_path)

        for rel_path in sorted(self._all_file_paths):
            abs_path = root / rel_path
            lang = _detect_language(rel_path)

            # Read file
            try:
                source = abs_path.read_bytes()
            except OSError:
                logger.debug("Could not read %s", rel_path)
                continue

            loc = source.count(b"\n") + (1 if source and not source.endswith(b"\n") else 0)

            # Create file node
            file_node = GraphNode(
                id=f"file:{rel_path}",
                kind=NodeKind.FILE,
                name=Path(rel_path).name,
                file_path=rel_path,
                language=lang,
                loc=loc,
            )
            self._graph.add_node(file_node)
            self._nx_graph.add_node(file_node.id)

            # Parse AST
            try:
                nodes, edges = _parse_file_ast(rel_path, lang, source)
                for node in nodes:
                    self._graph.add_node(node)
                for edge in edges:
                    self._graph.add_edge(edge)
            except Exception as e:
                logger.debug("AST parse failed for %s: %s", rel_path, e)

    def _resolve_imports(self) -> None:
        """Resolve import nodes to internal file paths and create dependency edges."""
        import_nodes_by_file: dict[str, list[GraphNode]] = defaultdict(list)
        for node in self._graph.nodes.values():
            if node.kind == NodeKind.IMPORT:
                import_nodes_by_file[node.file_path].append(node)

        for file_path, import_nodes in import_nodes_by_file.items():
            resolved = _resolve_internal_imports(
                file_path, import_nodes, self._all_file_paths, self.repo_path,
            )
            src_file_id = f"file:{file_path}"
            for dep_path in resolved:
                tgt_file_id = f"file:{dep_path}"
                self._graph.add_edge(GraphEdge(
                    source_id=src_file_id,
                    target_id=tgt_file_id,
                    kind=EdgeKind.DEPENDS_ON,
                ))
                # Also add to networkx graph for community detection
                self._nx_graph.add_edge(src_file_id, tgt_file_id)

    def _assign_segments_and_deps(self) -> None:
        """Assign segment IDs to nodes and compute inter-segment dependencies."""
        file_to_segment: dict[str, str] = {}
        for seg in self._graph.segments:
            for file_path in seg.files:
                file_to_segment[file_path] = seg.id

        # Assign segment_id to all nodes
        for node in self._graph.nodes.values():
            if node.file_path:
                node.segment_id = file_to_segment.get(node.file_path, "")

        # Compute inter-segment dependencies
        for seg in self._graph.segments:
            seg_file_set = set(seg.files)
            internal_deps = set()
            external_deps = set()

            for edge in self._graph.edges:
                if edge.kind != EdgeKind.DEPENDS_ON:
                    continue
                src_node = self._graph.nodes.get(edge.source_id)
                tgt_node = self._graph.nodes.get(edge.target_id)
                if not src_node or not tgt_node:
                    continue
                if src_node.file_path in seg_file_set:
                    if tgt_node.file_path not in seg_file_set:
                        tgt_seg = file_to_segment.get(tgt_node.file_path, "")
                        if tgt_seg:
                            internal_deps.add(tgt_seg)

            # Gather external deps from import nodes
            for file_path in seg.files:
                for node in self._graph.nodes.values():
                    if (
                        node.kind == NodeKind.IMPORT
                        and node.file_path == file_path
                    ):
                        module = node.metadata.get("module", node.name)
                        # If not resolved to an internal file, it's external
                        if not any(
                            e.source_id == f"file:{file_path}"
                            and e.kind == EdgeKind.DEPENDS_ON
                            and self._graph.nodes.get(e.target_id, GraphNode(id="", kind=NodeKind.FILE, name="")).file_path
                            for e in self._graph.edges
                        ):
                            if module:
                                external_deps.add(module.split(".")[0])

            seg.internal_deps = sorted(internal_deps)
            seg.external_deps = sorted(external_deps)[:20]  # Cap external deps

            # Compute entry points (functions that are referenced from outside)
            entry_points = []
            for node in self._graph.nodes.values():
                if (
                    node.kind == NodeKind.FUNCTION
                    and node.file_path in seg_file_set
                    and not node.name.startswith("_")
                ):
                    entry_points.append(f"{node.file_path}:{node.name}")
            seg.entry_points = entry_points[:20]  # Cap entry points

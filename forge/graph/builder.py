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


def _detect_language(file_path: str) -> str:
    """Detect language from file extension."""
    suffix = Path(file_path).suffix.lower()
    return _LANG_EXTENSIONS.get(suffix, "")


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


# ── AST Parsing ──────────────────────────────────────────────────────


def _parse_file_ast(
    file_path: str,
    language: str,
    source: bytes,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Parse a single file with tree-sitter and extract nodes + edges.

    Returns (nodes, edges) where nodes are functions/classes/imports
    and edges are contains/calls/imports relationships.
    """
    import tree_sitter as ts

    lang = _get_language(language)
    if lang is None:
        return [], []

    parser = ts.Parser(lang)
    tree = parser.parse(source)
    root = tree.root_node

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    # File node is created by the caller
    file_node_id = f"file:{file_path}"

    if language == "python":
        _extract_python(root, file_path, file_node_id, nodes, edges)
    elif language in ("javascript", "typescript"):
        _extract_js_ts(root, file_path, file_node_id, nodes, edges)
    elif language == "go":
        _extract_go(root, file_path, file_node_id, nodes, edges)
    elif language == "rust":
        _extract_rust(root, file_path, file_node_id, nodes, edges)
    elif language == "java":
        _extract_java(root, file_path, file_node_id, nodes, edges)
    elif language == "ruby":
        _extract_ruby(root, file_path, file_node_id, nodes, edges)

    return nodes, edges


def _extract_python(root, file_path, file_node_id, nodes, edges):
    """Extract Python AST: functions, classes, imports."""
    for child in root.children:
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            if name_node:
                fn_id = f"fn:{file_path}:{name_node.text.decode()}"
                nodes.append(GraphNode(
                    id=fn_id,
                    kind=NodeKind.FUNCTION,
                    name=name_node.text.decode(),
                    file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    language="python",
                ))
                edges.append(GraphEdge(
                    source_id=file_node_id,
                    target_id=fn_id,
                    kind=EdgeKind.CONTAINS,
                ))

        elif child.type == "class_definition":
            name_node = child.child_by_field_name("name")
            if name_node:
                cls_id = f"cls:{file_path}:{name_node.text.decode()}"
                nodes.append(GraphNode(
                    id=cls_id,
                    kind=NodeKind.CLASS,
                    name=name_node.text.decode(),
                    file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    language="python",
                ))
                edges.append(GraphEdge(
                    source_id=file_node_id,
                    target_id=cls_id,
                    kind=EdgeKind.CONTAINS,
                ))
                # Extract methods
                body = child.child_by_field_name("body")
                if body:
                    for member in body.children:
                        if member.type == "function_definition":
                            method_name_node = member.child_by_field_name("name")
                            if method_name_node:
                                method_id = f"fn:{file_path}:{name_node.text.decode()}.{method_name_node.text.decode()}"
                                nodes.append(GraphNode(
                                    id=method_id,
                                    kind=NodeKind.FUNCTION,
                                    name=f"{name_node.text.decode()}.{method_name_node.text.decode()}",
                                    file_path=file_path,
                                    line_start=member.start_point[0] + 1,
                                    line_end=member.end_point[0] + 1,
                                    language="python",
                                ))
                                edges.append(GraphEdge(
                                    source_id=cls_id,
                                    target_id=method_id,
                                    kind=EdgeKind.CONTAINS,
                                ))

        elif child.type in ("import_statement", "import_from_statement"):
            import_text = child.text.decode().strip()
            import_id = f"import:{file_path}:{hash(import_text) & 0xFFFFFF:06x}"
            # Extract module name
            module_name = ""
            if child.type == "import_from_statement":
                module_node = child.child_by_field_name("module_name")
                if module_node:
                    module_name = module_node.text.decode()
            else:
                # Regular import: import foo.bar
                for sub in child.children:
                    if sub.type == "dotted_name":
                        module_name = sub.text.decode()
                        break

            nodes.append(GraphNode(
                id=import_id,
                kind=NodeKind.IMPORT,
                name=module_name or import_text,
                file_path=file_path,
                line_start=child.start_point[0] + 1,
                language="python",
                metadata={"raw": import_text, "module": module_name},
            ))
            edges.append(GraphEdge(
                source_id=file_node_id,
                target_id=import_id,
                kind=EdgeKind.IMPORTS,
            ))


def _extract_js_ts(root, file_path, file_node_id, nodes, edges):
    """Extract JS/TS AST: functions, classes, imports."""
    def _walk(node, depth=0):
        if depth > 5:
            return
        for child in node.children:
            if child.type in ("function_declaration", "method_definition"):
                name_node = child.child_by_field_name("name")
                if name_node:
                    fn_id = f"fn:{file_path}:{name_node.text.decode()}"
                    nodes.append(GraphNode(
                        id=fn_id,
                        kind=NodeKind.FUNCTION,
                        name=name_node.text.decode(),
                        file_path=file_path,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        language=_detect_language(file_path),
                    ))
                    edges.append(GraphEdge(
                        source_id=file_node_id,
                        target_id=fn_id,
                        kind=EdgeKind.CONTAINS,
                    ))

            elif child.type == "class_declaration":
                name_node = child.child_by_field_name("name")
                if name_node:
                    cls_id = f"cls:{file_path}:{name_node.text.decode()}"
                    nodes.append(GraphNode(
                        id=cls_id,
                        kind=NodeKind.CLASS,
                        name=name_node.text.decode(),
                        file_path=file_path,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        language=_detect_language(file_path),
                    ))
                    edges.append(GraphEdge(
                        source_id=file_node_id,
                        target_id=cls_id,
                        kind=EdgeKind.CONTAINS,
                    ))

            elif child.type == "import_statement":
                source_node = child.child_by_field_name("source")
                module_name = source_node.text.decode().strip("'\"") if source_node else child.text.decode()
                import_id = f"import:{file_path}:{hash(module_name) & 0xFFFFFF:06x}"
                nodes.append(GraphNode(
                    id=import_id,
                    kind=NodeKind.IMPORT,
                    name=module_name,
                    file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    language=_detect_language(file_path),
                    metadata={"raw": child.text.decode(), "module": module_name},
                ))
                edges.append(GraphEdge(
                    source_id=file_node_id,
                    target_id=import_id,
                    kind=EdgeKind.IMPORTS,
                ))

            elif child.type in ("export_statement", "lexical_declaration", "variable_declaration"):
                _walk(child, depth + 1)

    _walk(root)


def _extract_go(root, file_path, file_node_id, nodes, edges):
    """Extract Go AST: functions, types, imports."""
    for child in root.children:
        if child.type == "function_declaration":
            name_node = child.child_by_field_name("name")
            if name_node:
                fn_id = f"fn:{file_path}:{name_node.text.decode()}"
                nodes.append(GraphNode(
                    id=fn_id, kind=NodeKind.FUNCTION,
                    name=name_node.text.decode(), file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1, language="go",
                ))
                edges.append(GraphEdge(
                    source_id=file_node_id, target_id=fn_id, kind=EdgeKind.CONTAINS,
                ))

        elif child.type == "type_declaration":
            for spec in child.children:
                if spec.type == "type_spec":
                    name_node = spec.child_by_field_name("name")
                    if name_node:
                        cls_id = f"cls:{file_path}:{name_node.text.decode()}"
                        nodes.append(GraphNode(
                            id=cls_id, kind=NodeKind.CLASS,
                            name=name_node.text.decode(), file_path=file_path,
                            line_start=spec.start_point[0] + 1,
                            line_end=spec.end_point[0] + 1, language="go",
                        ))
                        edges.append(GraphEdge(
                            source_id=file_node_id, target_id=cls_id,
                            kind=EdgeKind.CONTAINS,
                        ))

        elif child.type == "import_declaration":
            import_text = child.text.decode()
            import_id = f"import:{file_path}:{hash(import_text) & 0xFFFFFF:06x}"
            nodes.append(GraphNode(
                id=import_id, kind=NodeKind.IMPORT, name=import_text,
                file_path=file_path, line_start=child.start_point[0] + 1,
                language="go", metadata={"raw": import_text},
            ))
            edges.append(GraphEdge(
                source_id=file_node_id, target_id=import_id, kind=EdgeKind.IMPORTS,
            ))


def _extract_rust(root, file_path, file_node_id, nodes, edges):
    """Extract Rust AST: functions, structs/enums, use statements."""
    for child in root.children:
        if child.type == "function_item":
            name_node = child.child_by_field_name("name")
            if name_node:
                fn_id = f"fn:{file_path}:{name_node.text.decode()}"
                nodes.append(GraphNode(
                    id=fn_id, kind=NodeKind.FUNCTION,
                    name=name_node.text.decode(), file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1, language="rust",
                ))
                edges.append(GraphEdge(
                    source_id=file_node_id, target_id=fn_id, kind=EdgeKind.CONTAINS,
                ))

        elif child.type in ("struct_item", "enum_item"):
            name_node = child.child_by_field_name("name")
            if name_node:
                cls_id = f"cls:{file_path}:{name_node.text.decode()}"
                nodes.append(GraphNode(
                    id=cls_id, kind=NodeKind.CLASS,
                    name=name_node.text.decode(), file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1, language="rust",
                ))
                edges.append(GraphEdge(
                    source_id=file_node_id, target_id=cls_id, kind=EdgeKind.CONTAINS,
                ))

        elif child.type == "use_declaration":
            use_text = child.text.decode()
            import_id = f"import:{file_path}:{hash(use_text) & 0xFFFFFF:06x}"
            nodes.append(GraphNode(
                id=import_id, kind=NodeKind.IMPORT, name=use_text,
                file_path=file_path, line_start=child.start_point[0] + 1,
                language="rust", metadata={"raw": use_text},
            ))
            edges.append(GraphEdge(
                source_id=file_node_id, target_id=import_id, kind=EdgeKind.IMPORTS,
            ))


def _extract_java(root, file_path, file_node_id, nodes, edges):
    """Extract Java AST: methods, classes, imports."""
    def _walk(node, depth=0):
        if depth > 5:
            return
        for child in node.children:
            if child.type == "method_declaration":
                name_node = child.child_by_field_name("name")
                if name_node:
                    fn_id = f"fn:{file_path}:{name_node.text.decode()}"
                    nodes.append(GraphNode(
                        id=fn_id, kind=NodeKind.FUNCTION,
                        name=name_node.text.decode(), file_path=file_path,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1, language="java",
                    ))
                    edges.append(GraphEdge(
                        source_id=file_node_id, target_id=fn_id, kind=EdgeKind.CONTAINS,
                    ))

            elif child.type == "class_declaration":
                name_node = child.child_by_field_name("name")
                if name_node:
                    cls_id = f"cls:{file_path}:{name_node.text.decode()}"
                    nodes.append(GraphNode(
                        id=cls_id, kind=NodeKind.CLASS,
                        name=name_node.text.decode(), file_path=file_path,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1, language="java",
                    ))
                    edges.append(GraphEdge(
                        source_id=file_node_id, target_id=cls_id, kind=EdgeKind.CONTAINS,
                    ))
                    _walk(child, depth + 1)

            elif child.type == "import_declaration":
                import_text = child.text.decode()
                import_id = f"import:{file_path}:{hash(import_text) & 0xFFFFFF:06x}"
                nodes.append(GraphNode(
                    id=import_id, kind=NodeKind.IMPORT, name=import_text,
                    file_path=file_path, line_start=child.start_point[0] + 1,
                    language="java", metadata={"raw": import_text},
                ))
                edges.append(GraphEdge(
                    source_id=file_node_id, target_id=import_id, kind=EdgeKind.IMPORTS,
                ))

            elif child.type == "program":
                _walk(child, depth + 1)

    _walk(root)


def _extract_ruby(root, file_path, file_node_id, nodes, edges):
    """Extract Ruby AST: methods, classes, requires."""
    for child in root.children:
        if child.type == "method":
            name_node = child.child_by_field_name("name")
            if name_node:
                fn_id = f"fn:{file_path}:{name_node.text.decode()}"
                nodes.append(GraphNode(
                    id=fn_id, kind=NodeKind.FUNCTION,
                    name=name_node.text.decode(), file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1, language="ruby",
                ))
                edges.append(GraphEdge(
                    source_id=file_node_id, target_id=fn_id, kind=EdgeKind.CONTAINS,
                ))

        elif child.type == "class":
            name_node = child.child_by_field_name("name")
            if name_node:
                cls_id = f"cls:{file_path}:{name_node.text.decode()}"
                nodes.append(GraphNode(
                    id=cls_id, kind=NodeKind.CLASS,
                    name=name_node.text.decode(), file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1, language="ruby",
                ))
                edges.append(GraphEdge(
                    source_id=file_node_id, target_id=cls_id, kind=EdgeKind.CONTAINS,
                ))


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


# ── Community Detection / Segmentation ───────────────────────────────


def _segment_by_community_detection(
    graph: CodeGraph,
    nx_graph: nx.Graph,
    target_segments: int = 5,
    min_segment_size: int = 2,
) -> list[Segment]:
    """Use modularity-based community detection to cluster files.

    Falls back to directory-based segmentation if the graph is too sparse.
    """
    file_nodes = [
        n for n in graph.nodes.values()
        if n.kind == NodeKind.FILE
    ]

    if len(file_nodes) <= target_segments:
        # Too few files — single segment
        seg = Segment(
            id="seg-all",
            label="all",
            files=[n.file_path for n in file_nodes],
            node_ids=[n.id for n in file_nodes],
            loc=sum(n.loc for n in file_nodes),
        )
        return [seg]

    # Build undirected graph of file dependencies
    file_graph = nx.Graph()
    file_id_to_path = {n.id: n.file_path for n in file_nodes}
    file_path_to_id = {v: k for k, v in file_id_to_path.items()}

    for fid in file_id_to_path:
        file_graph.add_node(fid)

    # Add edges from import relationships
    for edge in graph.edges:
        if edge.kind in (EdgeKind.IMPORTS, EdgeKind.CALLS, EdgeKind.REFERENCES):
            src_file = None
            tgt_file = None

            src_node = graph.nodes.get(edge.source_id)
            tgt_node = graph.nodes.get(edge.target_id)

            if src_node and src_node.file_path:
                src_file = file_path_to_id.get(f"file:{src_node.file_path}")
            if tgt_node and tgt_node.file_path:
                tgt_file = file_path_to_id.get(f"file:{tgt_node.file_path}")

            if src_file and tgt_file and src_file != tgt_file:
                if file_graph.has_edge(src_file, tgt_file):
                    file_graph[src_file][tgt_file]["weight"] += 1
                else:
                    file_graph.add_edge(src_file, tgt_file, weight=1)

    # Try Louvain community detection
    if file_graph.number_of_edges() >= len(file_nodes) // 2:
        try:
            communities = nx.community.louvain_communities(
                file_graph,
                resolution=1.0,
                seed=42,
            )
            return _communities_to_segments(graph, communities, file_id_to_path)
        except Exception as e:
            logger.warning("Louvain community detection failed: %s, falling back to directory-based", e)

    # Fallback: directory-based segmentation
    return _segment_by_directory(graph, file_nodes, target_segments)


def _communities_to_segments(
    graph: CodeGraph,
    communities: list[set[str]],
    file_id_to_path: dict[str, str],
) -> list[Segment]:
    """Convert networkx communities to Segment objects."""
    segments = []
    for i, community in enumerate(communities):
        file_paths = [file_id_to_path[fid] for fid in community if fid in file_id_to_path]
        if not file_paths:
            continue

        # Derive a label from common directory prefix
        common = os.path.commonpath(file_paths) if len(file_paths) > 1 else str(Path(file_paths[0]).parent)
        label = Path(common).name or f"cluster-{i}"

        # Collect all node IDs in this segment
        node_ids = list(community)
        for fid in community:
            file_path = file_id_to_path.get(fid, "")
            for n in graph.nodes.values():
                if n.file_path == file_path and n.id not in node_ids:
                    node_ids.append(n.id)

        seg = Segment(
            id=f"seg-{label}-{i}",
            label=label,
            files=sorted(file_paths),
            node_ids=node_ids,
            loc=sum(graph.nodes[nid].loc for nid in community if nid in graph.nodes),
        )
        segments.append(seg)

    return segments


def _segment_by_directory(
    graph: CodeGraph,
    file_nodes: list[GraphNode],
    target_segments: int,
) -> list[Segment]:
    """Fallback segmentation based on top-level directory structure."""
    dir_groups: dict[str, list[GraphNode]] = defaultdict(list)

    for node in file_nodes:
        parts = Path(node.file_path).parts
        if len(parts) >= 2:
            top_dir = parts[0]
        else:
            top_dir = "root"
        dir_groups[top_dir].append(node)

    segments = []
    for i, (dir_name, nodes) in enumerate(sorted(dir_groups.items())):
        node_ids = [n.id for n in nodes]
        # Also gather non-file nodes in these files
        file_paths = {n.file_path for n in nodes}
        for n in graph.nodes.values():
            if n.file_path in file_paths and n.id not in node_ids:
                node_ids.append(n.id)

        seg = Segment(
            id=f"seg-{dir_name}-{i}",
            label=dir_name,
            files=sorted(n.file_path for n in nodes),
            node_ids=node_ids,
            loc=sum(n.loc for n in nodes),
        )
        segments.append(seg)

    return segments


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

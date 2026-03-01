"""Language-specific AST extraction functions for the Code Graph Builder.

Each extractor takes a tree-sitter root node and populates graph nodes
and edges for functions, classes, and imports in that language.

Extracted from builder.py to separate per-language parsing logic
from the orchestration and segmentation code.
"""

from __future__ import annotations

import logging
from pathlib import Path

from forge.graph.models import (
    EdgeKind,
    GraphEdge,
    GraphNode,
    NodeKind,
)

logger = logging.getLogger(__name__)

# Extension → language mapping (shared with builder.py)
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


def _detect_language(file_path: str) -> str:
    """Detect language from file extension."""
    suffix = Path(file_path).suffix.lower()
    return _LANG_EXTENSIONS.get(suffix, "")


def _parse_file_ast(
    file_path: str,
    language: str,
    source: bytes,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Parse a single file with tree-sitter and extract nodes + edges.

    Returns (nodes, edges) where nodes are functions/classes/imports
    and edges are contains/calls/imports relationships.
    """
    from forge.graph.builder import _get_language
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

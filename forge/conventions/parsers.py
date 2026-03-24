"""Deterministic config file parsers for convention extraction.

Each parser reads a specific config file type and extracts signals
that affect finding suppression. All parsers are defensive — file
read or parse failures return empty results (never crash).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _read_json_safe(path: Path) -> dict:
    """Read and parse a JSON file, stripping JS-style comments and trailing commas."""
    try:
        text = path.read_text(errors="replace")
        # Strip single-line comments (// ...)
        text = re.sub(r'//.*?$', '', text, flags=re.MULTILINE)
        # Strip multi-line comments (/* ... */)
        text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
        # Strip trailing commas before } or ]
        text = re.sub(r',\s*([}\]])', r'\1', text)
        return json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.debug("Failed to read JSON %s: %s", path, e)
        return {}


def _read_toml_safe(path: Path) -> dict:
    """Read and parse a TOML file."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # Python < 3.11 fallback
        except ImportError:
            logger.debug("No TOML parser available, skipping %s", path)
            return {}
    try:
        return tomllib.loads(path.read_text(errors="replace"))
    except (OSError, Exception) as e:
        logger.debug("Failed to read TOML %s: %s", path, e)
        return {}


def _read_yaml_safe(path: Path) -> dict:
    """Read and parse a YAML file."""
    try:
        import yaml
        return yaml.safe_load(path.read_text(errors="replace")) or {}
    except (ImportError, OSError, Exception) as e:
        logger.debug("Failed to read YAML %s: %s", path, e)
        return {}


def _read_ini_safe(path: Path) -> dict[str, dict[str, str]]:
    """Read and parse an INI-style config file."""
    try:
        import configparser
        cp = configparser.ConfigParser()
        cp.read(str(path))
        return {s: dict(cp[s]) for s in cp.sections()}
    except (OSError, Exception) as e:
        logger.debug("Failed to read INI %s: %s", path, e)
        return {}


# ── ESLint ───────────────────────────────────────────────────────────


def parse_eslint(repo_path: str) -> dict:
    """Extract disabled rules from ESLint config.

    Searches for: .eslintrc.json, .eslintrc, .eslintrc.yml,
    .eslintrc.yaml, eslint.config.js, eslint.config.mjs

    Returns:
        {"disabled_rules": [...], "config_file": "..."}
    """
    root = Path(repo_path)
    disabled: list[str] = []
    config_file = ""

    # JSON configs
    for name in (".eslintrc.json", ".eslintrc"):
        path = root / name
        if path.exists():
            data = _read_json_safe(path)
            if data:
                disabled.extend(_extract_eslint_disabled(data))
                config_file = name
                break

    # YAML configs
    if not config_file:
        for name in (".eslintrc.yml", ".eslintrc.yaml"):
            path = root / name
            if path.exists():
                data = _read_yaml_safe(path)
                if data:
                    disabled.extend(_extract_eslint_disabled(data))
                    config_file = name
                    break

    # Flat config (eslint.config.js/mjs) — can only detect via regex
    if not config_file:
        for name in ("eslint.config.js", "eslint.config.mjs"):
            path = root / name
            if path.exists():
                try:
                    text = path.read_text(errors="replace")[:10000]
                    off_rules = re.findall(
                        r'["\'](@?[\w/-]+)["\']\s*:\s*["\']off["\']',
                        text,
                    )
                    off_rules += re.findall(
                        r'["\'](@?[\w/-]+)["\']\s*:\s*0\b',
                        text,
                    )
                    disabled.extend(off_rules)
                    config_file = name
                except OSError as e:
                    logger.debug("Failed to read flat ESLint config %s: %s", name, e)
                break

    # Also check package.json eslintConfig
    if not config_file:
        pkg = root / "package.json"
        if pkg.exists():
            data = _read_json_safe(pkg)
            eslint_cfg = data.get("eslintConfig", {})
            if eslint_cfg:
                disabled.extend(_extract_eslint_disabled(eslint_cfg))
                config_file = "package.json[eslintConfig]"

    return {"disabled_rules": sorted(set(disabled)), "config_file": config_file}


def _extract_eslint_disabled(data: dict) -> list[str]:
    """Extract rules set to 'off' or 0 from ESLint config dict."""
    disabled = []
    rules = data.get("rules", {})
    for rule, value in rules.items():
        if value == "off" or value == 0:
            disabled.append(rule)
        elif isinstance(value, list) and len(value) > 0:
            if value[0] == "off" or value[0] == 0:
                disabled.append(rule)
    return disabled


# ── TypeScript ───────────────────────────────────────────────────────


def parse_tsconfig(repo_path: str) -> dict:
    """Extract conventions from tsconfig.json.

    Returns:
        {"strict": bool|None, "no_implicit_any": bool|None,
         "target": str, "jsx": str, "config_file": str}
    """
    root = Path(repo_path)
    path = root / "tsconfig.json"
    if not path.exists():
        return {}

    data = _read_json_safe(path)
    if not data:
        return {}

    compiler = data.get("compilerOptions", {})
    result: dict[str, Any] = {"config_file": "tsconfig.json"}

    if "strict" in compiler:
        result["strict"] = bool(compiler["strict"])
    if "noImplicitAny" in compiler:
        result["no_implicit_any"] = bool(compiler["noImplicitAny"])
    if "target" in compiler:
        result["target"] = str(compiler["target"])
    if "jsx" in compiler:
        result["jsx"] = str(compiler["jsx"])

    return result


# ── Python: pyproject.toml [tool.ruff] / [tool.pylint] ──────────────


def parse_pyproject_toml(repo_path: str) -> dict:
    """Extract linting + testing conventions from pyproject.toml.

    Parses:
      - [tool.ruff] / [tool.ruff.lint] -> ignored rules, line-length, target-version
      - [tool.pylint] -> disabled rules
      - [tool.pytest.ini_options] -> markers, testpaths
      - [tool.coverage.report] -> fail_under

    Returns:
        {"lint": {...}, "test": {...}}
    """
    root = Path(repo_path)
    path = root / "pyproject.toml"
    if not path.exists():
        return {}

    data = _read_toml_safe(path)
    if not data:
        return {}

    tool = data.get("tool", {})
    result: dict[str, Any] = {}

    # Ruff
    ruff = tool.get("ruff", {})
    if ruff:
        lint_section = ruff.get("lint", ruff)
        ignored = lint_section.get("ignore", [])
        line_length = ruff.get("line-length", ruff.get("line_length"))
        target = ruff.get("target-version", ruff.get("target_version", ""))
        result["lint"] = {
            "tool": "ruff",
            "disabled_rules": [str(r) for r in ignored],
            "line_length": line_length,
            "target_version": str(target) if target else "",
            "config_file": "pyproject.toml[tool.ruff]",
        }

    # Pylint
    pylint = tool.get("pylint", {})
    if pylint and "lint" not in result:
        messages = pylint.get("messages_control", pylint.get("messages-control", {}))
        disabled = messages.get("disable", [])
        if isinstance(disabled, str):
            disabled = [d.strip() for d in disabled.split(",")]
        result["lint"] = {
            "tool": "pylint",
            "disabled_rules": disabled,
            "config_file": "pyproject.toml[tool.pylint]",
        }

    # Pytest
    pytest_cfg = tool.get("pytest", {}).get("ini_options", {})
    if pytest_cfg:
        markers_raw = pytest_cfg.get("markers", [])
        markers = []
        for m in markers_raw:
            name = m.split(":")[0].strip() if isinstance(m, str) else str(m)
            markers.append(name)
        testpaths = pytest_cfg.get("testpaths", [])
        result["test"] = {
            "framework": "pytest",
            "custom_markers": markers,
            "test_paths": testpaths,
            "config_file": "pyproject.toml[tool.pytest]",
        }

    # Coverage threshold
    coverage = tool.get("coverage", {}).get("report", {})
    fail_under = coverage.get("fail_under")
    if fail_under is not None and "test" in result:
        result["test"]["coverage_threshold"] = float(fail_under)

    return result


# ── Python: pytest.ini / setup.cfg ───────────────────────────────────


def parse_pytest_ini(repo_path: str) -> dict:
    """Extract pytest conventions from pytest.ini or setup.cfg.

    Returns:
        {"framework": "pytest", "custom_markers": [...], "test_paths": [...],
         "config_file": "..."}
    """
    root = Path(repo_path)

    # pytest.ini
    path = root / "pytest.ini"
    if path.exists():
        sections = _read_ini_safe(path)
        pytest_section = sections.get("pytest", {})
        if pytest_section:
            return _extract_pytest_from_ini(pytest_section, "pytest.ini")

    # setup.cfg [tool:pytest]
    path = root / "setup.cfg"
    if path.exists():
        sections = _read_ini_safe(path)
        pytest_section = sections.get("tool:pytest", {})
        if pytest_section:
            return _extract_pytest_from_ini(pytest_section, "setup.cfg[tool:pytest]")

    return {}


def _extract_pytest_from_ini(section: dict, config_file: str) -> dict:
    """Extract pytest conventions from an INI section."""
    markers = []
    markers_raw = section.get("markers", "")
    for line in markers_raw.strip().split("\n"):
        line = line.strip()
        if line:
            name = line.split(":")[0].strip()
            if name:
                markers.append(name)

    testpaths: list[str] = []
    testpaths_raw = section.get("testpaths", "")
    for line in testpaths_raw.strip().split("\n"):
        line = line.strip()
        if line:
            testpaths.append(line)

    return {
        "framework": "pytest",
        "custom_markers": markers,
        "test_paths": testpaths,
        "config_file": config_file,
    }


# ── Python: .pylintrc / .flake8 ─────────────────────────────────────


def parse_pylintrc(repo_path: str) -> dict:
    """Extract disabled rules from .pylintrc.

    Returns:
        {"tool": "pylint", "disabled_rules": [...], "config_file": "..."}
    """
    root = Path(repo_path)
    path = root / ".pylintrc"
    if not path.exists():
        return {}

    sections = _read_ini_safe(path)
    messages = sections.get("MESSAGES CONTROL", sections.get("messages control", {}))
    disabled_raw = messages.get("disable", "")
    disabled = [d.strip() for d in disabled_raw.split(",") if d.strip()]

    return {
        "tool": "pylint",
        "disabled_rules": disabled,
        "config_file": ".pylintrc",
    }


def parse_flake8(repo_path: str) -> dict:
    """Extract ignored rules from .flake8 or setup.cfg [flake8].

    Returns:
        {"tool": "flake8", "disabled_rules": [...], "line_length": int|None,
         "config_file": "..."}
    """
    root = Path(repo_path)

    # .flake8
    path = root / ".flake8"
    if path.exists():
        sections = _read_ini_safe(path)
        flake8 = sections.get("flake8", {})
        if flake8:
            return _extract_flake8(flake8, ".flake8")

    # setup.cfg [flake8]
    path = root / "setup.cfg"
    if path.exists():
        sections = _read_ini_safe(path)
        flake8 = sections.get("flake8", {})
        if flake8:
            return _extract_flake8(flake8, "setup.cfg[flake8]")

    return {}


def _extract_flake8(section: dict, config_file: str) -> dict:
    """Extract flake8 conventions from config section."""
    ignored_raw = section.get("ignore", section.get("extend-ignore", ""))
    ignored = [r.strip() for r in ignored_raw.split(",") if r.strip()]
    line_length = None
    ll_raw = section.get("max-line-length", section.get("max_line_length"))
    if ll_raw:
        try:
            line_length = int(ll_raw)
        except ValueError:
            pass
    return {
        "tool": "flake8",
        "disabled_rules": ignored,
        "line_length": line_length,
        "config_file": config_file,
    }


# ── JavaScript: Jest config ─────────────────────────────────────────


def parse_jest_config(repo_path: str) -> dict:
    """Extract testing conventions from Jest config.

    Searches: jest.config.js, jest.config.ts, package.json[jest]

    Returns:
        {"framework": "jest", "test_paths": [...],
         "coverage_threshold": float|None, "config_file": str}
    """
    root = Path(repo_path)

    # jest.config.js / jest.config.ts
    for name in ("jest.config.js", "jest.config.ts", "jest.config.mjs"):
        path = root / name
        if path.exists():
            try:
                text = path.read_text(errors="replace")[:10000]
                result: dict[str, Any] = {"framework": "jest", "config_file": name}
                roots = re.findall(r'roots\s*:\s*\[([^\]]+)\]', text)
                if roots:
                    paths = re.findall(r'["\']([^"\']+)["\']', roots[0])
                    result["test_paths"] = paths
                threshold = re.search(r'global\s*:\s*\{\s*lines\s*:\s*(\d+)', text)
                if threshold:
                    result["coverage_threshold"] = float(threshold.group(1))
                return result
            except OSError as e:
                logger.debug("Failed to read Jest config %s: %s", name, e)

    # package.json[jest]
    pkg = root / "package.json"
    if pkg.exists():
        data = _read_json_safe(pkg)
        jest_cfg = data.get("jest", {})
        if jest_cfg:
            result = {"framework": "jest", "config_file": "package.json[jest]"}
            roots = jest_cfg.get("roots", jest_cfg.get("testMatch", []))
            if isinstance(roots, list):
                result["test_paths"] = roots
            coverage = jest_cfg.get("coverageThreshold", {}).get("global", {})
            if coverage:
                lines = coverage.get("lines", coverage.get("statements"))
                if lines is not None:
                    result["coverage_threshold"] = float(lines)
            return result

    return {}


# ── Prettier ─────────────────────────────────────────────────────────


def parse_prettier(repo_path: str) -> str:
    """Detect if Prettier is used as the formatter.

    Returns formatter name or empty string.
    """
    root = Path(repo_path)
    for name in (".prettierrc", ".prettierrc.json", ".prettierrc.js",
                 ".prettierrc.yml", ".prettierrc.yaml", "prettier.config.js"):
        if (root / name).exists():
            return "prettier"

    pkg = root / "package.json"
    if pkg.exists():
        data = _read_json_safe(pkg)
        if "prettier" in data:
            return "prettier"
        deps = {**data.get("devDependencies", {}), **data.get("dependencies", {})}
        if "prettier" in deps:
            return "prettier"

    return ""

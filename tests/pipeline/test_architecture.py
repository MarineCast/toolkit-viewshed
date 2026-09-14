from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

PACKAGE_NAME = "viewshed_toolkit.pipeline"
PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "viewshed_toolkit" / "pipeline"
TOOLKIT_ROOT = PACKAGE_ROOT.parent


def _python_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT)
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join([PACKAGE_NAME, *parts]) if parts else PACKAGE_NAME
        modules[module] = path
    return modules


def _internal_dependencies(module: str, path: Path, known: set[str]) -> set[str]:
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    dependencies: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(prefix, package)
            else:
                base = node.module or ""
            candidates = (
                [f"{base}.{alias.name}" for alias in node.names] if not node.module else [base]
            )
        else:
            continue
        for candidate in candidates:
            if candidate in known and candidate != module:
                dependencies.add(candidate)
    return dependencies


def test_viewshed_uses_explicit_imports_and_layering() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                violations.append(f"{relative}:{node.lineno}: wildcard import")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "globals"
            ):
                violations.append(f"{relative}:{node.lineno}: globals().update")
            if relative.parts[:1] == ("prepare",) and isinstance(node, ast.ImportFrom):
                imported = "." * node.level + (node.module or "")
                if "weights" in imported.split("."):
                    violations.append(f"{relative}:{node.lineno}: prepare depends on weights")
    assert not violations, "\n".join(violations)


def test_viewshed_internal_import_graph_is_acyclic() -> None:
    modules = _python_modules()
    graph = {
        module: _internal_dependencies(module, path, set(modules))
        for module, path in modules.items()
    }
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(module: str) -> None:
        if module in visiting:
            start = visiting.index(module)
            cycle = [*visiting[start:], module]
            raise AssertionError("viewshed import cycle: " + " -> ".join(cycle))
        if module in visited:
            return
        visiting.append(module)
        for dependency in sorted(graph[module]):
            visit(dependency)
        visiting.pop()
        visited.add(module)

    for module in sorted(graph):
        visit(module)


def test_viewshed_api_is_independent_of_cli_and_argparse() -> None:
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "api").rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            for imported in imports:
                if imported == "argparse" or "cli" in imported.split("."):
                    violations.append(f"{relative}:{node.lineno}: imports {imported}")
    assert not violations, "\n".join(violations)


def test_early_pipeline_phases_do_not_depend_on_finalization() -> None:
    violations: list[str] = []
    for area in ("config", "prepare", "diagnostics"):
        for path in (PACKAGE_ROOT / area).rglob("*.py"):
            relative = path.relative_to(PACKAGE_ROOT)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                imported = "." * node.level + (node.module or "")
                if "finalize" in imported.split("."):
                    violations.append(f"{relative}:{node.lineno}: imports {imported}")
    assert not violations, "\n".join(violations)


def test_internal_infrastructure_does_not_depend_on_pipeline() -> None:
    violations: list[str] = []
    for path in (TOOLKIT_ROOT / "_internal").rglob("*.py"):
        relative = path.relative_to(TOOLKIT_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            for imported in imports:
                if imported.startswith("viewshed_toolkit.pipeline"):
                    violations.append(f"{relative}:{node.lineno}: imports {imported}")
    assert not violations, "\n".join(violations)


def test_pair_contract_constants_have_one_owner() -> None:
    owned_names = {
        "DISTANCE_OUTPUT_SCHEMA",
        "LOOKUP_ALGORITHM_VERSION",
        "SOURCE_TARGET_LOOKUP_SCHEMA",
    }
    owner = PACKAGE_ROOT / "contracts" / "pairs.py"
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == owner:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in owned_names:
                        violations.append(
                            f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}: redefines {target.id}"
                        )
    assert not violations, "\n".join(violations)


def test_top_level_package_has_no_pass_through_modules() -> None:
    top_level_modules = {path.name for path in TOOLKIT_ROOT.glob("*.py")}

    assert top_level_modules == {"__init__.py", "__main__.py"}


def test_removed_compatibility_aliases_stay_removed() -> None:
    assert not (TOOLKIT_ROOT / "_internal" / "data" / "persistence.py").exists()
    assert "validate_final_artifact" not in (
        TOOLKIT_ROOT / "_internal" / "data" / "parquet.py"
    ).read_text(encoding="utf-8")
    assert "def resolve_existing_or_relative(" not in (
        PACKAGE_ROOT / "config" / "paths.py"
    ).read_text(encoding="utf-8")
    assert "def _stage_invocations(" not in (PACKAGE_ROOT / "api" / "service.py").read_text(
        encoding="utf-8"
    )

    h3_source = (TOOLKIT_ROOT / "_internal" / "geo" / "h3.py").read_text(encoding="utf-8")
    for alias in ("to_parent", "to_children", "to_boundary", "to_latlng", "to_polygon"):
        assert f"\n{alias} =" not in h3_source

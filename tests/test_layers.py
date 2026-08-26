"""The dependency direction between layers, enforced.

    domain  <-  infrastructure  <-  services  <-  api

Arrows point one way only, which is what keeps the suite running in two seconds
without a network. Decay here is one import line and goes unnoticed for months,
so the check runs with every `pytest` rather than in a script.
"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"

# Modules allowed to know about everything, because they wire it together.
COMPOSITION_ROOT = {"src.core.lifespan", "src.main", "src"}
LAYERS = ("api", "services", "infrastructure", "domain", "core")

# Layers a given layer must never import from.
FORBIDDEN: dict[str, set[str]] = {
    "domain": {"api", "services", "infrastructure", "core", "composition-root"},
    "infrastructure": {"api", "services", "composition-root"},
    "services": {"api", "composition-root"},
    "core": {"api", "services", "infrastructure", "domain", "composition-root"},
    "api": set(),
    "composition-root": set(),
}

# Packages that must never reach the pure-logic layer.
FORBIDDEN_THIRD_PARTY: dict[str, set[str]] = {
    "domain": {"fastapi", "starlette", "langchain", "openai", "anthropic", "httpx", "msal"},
}


def test_no_layer_imports_from_above_it() -> None:
    violations = find_violations()
    assert not violations, "\n" + "\n".join(violations)


def test_the_checker_actually_looked_at_the_code() -> None:
    """A broken glob would make every check above pass on nothing."""
    modules = list(SOURCE_ROOT.rglob("*.py"))

    assert len(modules) > 20
    assert any(path.parts[-2] == "domain" for path in modules)


def find_violations() -> list[str]:
    """Every import that crosses a layer boundary the wrong way."""
    violations = []

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        source = module_name(path)
        layer = layer_of(source)

        for imported in imports_of(path):
            root = imported.split(".")[0]
            if root in FORBIDDEN_THIRD_PARTY.get(layer, set()):
                violations.append(f"{source} -> {imported}   [{layer} must not import {root}]")
            elif imported.startswith("src") and layer_of(imported) in FORBIDDEN[layer]:
                violations.append(f"{source} -> {imported}   [{layer} -> {layer_of(imported)}]")

    return violations


def imports_of(path: Path) -> list[str]:
    """Names this module imports, read from the syntax tree rather than by regex."""
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def module_name(path: Path) -> str:
    relative = path.relative_to(PROJECT_ROOT).with_suffix("")
    return ".".join(relative.parts).removesuffix(".__init__")


def layer_of(module: str) -> str:
    if module in COMPOSITION_ROOT:
        return "composition-root"
    for name in LAYERS:
        if module.startswith(f"src.{name}"):
            return name
    return "composition-root"

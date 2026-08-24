"""The ``aeic`` extra must be genuinely optional.

``app/services/messages.py`` imports ``note_inbound_chunk`` at module level, so
the AEIC package sits on the import path of the entire application. If importing
it requires numpy, a base install cannot start **at all** -- uvicorn dies with
``ModuleNotFoundError: No module named 'numpy'`` before the radio connects.

That shipped once, and no test caught it: numpy is installed in the dev
environment and in CI, so every import succeeded there. The only way to catch it
without a second dependency-free environment is to check the import graph
*statically*, which is what this file does.

The rule: nothing reachable at import time from the modules the app touches
(``__init__``, ``service``, ``ingest``, ``transport``, ``text_transport``,
``prepare``, ``constants``, ``bundle``, ``png``, ``rans``, ``tables``) may import
numpy, onnxruntime or PIL at module level. The heavy modules (``entropy``,
``onnx_backend``) are reached lazily, inside the functions that run inference.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_DIR = Path("app/imaging/aeic")

HEAVY_MODULES = {"numpy", "onnxruntime", "PIL"}

# Modules the running app imports, directly or transitively, at import time.
# Adding one here asserts it stays importable without the extra.
LIGHT_MODULES = (
    "__init__",
    "constants",
    "bundle",
    "channel_data",
    "channel_data_ingest",
    "ingest",
    "png",
    "prepare",
    "rans",
    "service",
    "tables",
    "text_transport",
    "transport",
)

# These may import numpy/ORT at module level; nothing light may reach them
# at import time.
HEAVY_LOCAL_MODULES = {"entropy", "onnx_backend"}


def module_path(name: str) -> Path:
    return PACKAGE_DIR / f"{name}.py"


def top_level_imports(path: Path) -> set[str]:
    """Module names imported at MODULE level (not inside a function or class).

    Deliberately ignores imports nested in a function body: that is exactly the
    lazy-import pattern the heavy modules are reached through, and the whole
    point of the split.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in tree.body:  # top level only
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import
                continue
            if node.module:
                found.add(node.module)
        elif isinstance(node, ast.If):
            # `if TYPE_CHECKING:` blocks never execute at runtime.
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    continue
    return found


def local_dependencies(path: Path) -> set[str]:
    """Sibling modules in this package imported at module level."""
    prefix = "app.imaging.aeic."
    return {
        name[len(prefix) :].split(".")[0]
        for name in top_level_imports(path)
        if name.startswith(prefix)
    }


@pytest.mark.parametrize("name", LIGHT_MODULES)
def test_light_module_does_not_import_a_heavy_dependency(name):
    path = module_path(name)
    assert path.is_file(), f"{path} is missing"
    offenders = {
        imported.split(".")[0]
        for imported in top_level_imports(path)
        if imported.split(".")[0] in HEAVY_MODULES
    }
    assert not offenders, (
        f"{path} imports {sorted(offenders)} at module level. The AEIC package is on "
        "the app's import path (app/services/messages.py), so this makes the whole "
        "app fail to start without the 'aeic' extra. Import it inside the function "
        "that needs it instead."
    )


@pytest.mark.parametrize("name", LIGHT_MODULES)
def test_light_module_does_not_reach_a_heavy_module_at_import_time(name):
    """Transitive guard: importing a heavy sibling is as fatal as importing numpy."""
    offenders = local_dependencies(module_path(name)) & HEAVY_LOCAL_MODULES
    assert not offenders, (
        f"{module_path(name)} imports {sorted(offenders)} at module level, and those "
        "import numpy. Move it into the function that needs it."
    )


def test_the_heavy_modules_are_the_only_ones_that_need_the_extra():
    """Documents the split, and fails if a new module appears unclassified."""
    present = {path.stem for path in PACKAGE_DIR.glob("*.py") if path.stem != "__pycache__"}
    classified = set(LIGHT_MODULES) | HEAVY_LOCAL_MODULES
    unclassified = present - classified
    assert not unclassified, (
        f"new AEIC module(s) {sorted(unclassified)} are not classified. Add them to "
        "LIGHT_MODULES (and keep them free of numpy/ORT/PIL) or to "
        "HEAVY_LOCAL_MODULES if they legitimately need the extra."
    )


def test_the_app_entry_point_reaches_aeic_through_a_light_module_only():
    """The actual failure path, asserted at its source.

    ``app/services/messages.py`` is what puts AEIC on the app's import path.
    """
    source = Path("app/services/messages.py").read_text(encoding="utf-8")
    assert "from app.imaging.aeic.ingest import note_inbound_chunk" in source
    assert "ingest" in LIGHT_MODULES


def test_constants_module_is_stdlib_only():
    """It exists precisely so the light modules have somewhere to get shapes and
    the runtime probe from."""
    imports = top_level_imports(module_path("constants"))
    assert not (imports & HEAVY_MODULES)
    assert not local_dependencies(module_path("constants"))

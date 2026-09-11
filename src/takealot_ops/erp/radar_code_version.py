"""Hash the radar projection's Python dependencies, excluding unrelated handlers."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
from pathlib import Path


def materialized_code_fingerprint(root: Path) -> str:
    if not (root / "src/takealot_ops/erp/web.py").is_file():
        root = Path(__file__).resolve().parents[3]
    source = root / "src"
    web_path = source / "takealot_ops/erp/web.py"
    tree = ast.parse(web_path.read_text(encoding="utf-8-sig"))
    # Local helper calls are followed recursively, including nested read handlers.
    symbols: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name != "create_app":
                symbols[node.name] = node
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    symbols[target.id] = node

    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name

    pending = ["competitors", "own_store_competitors", "materialized_radar_response",
               "_own_store_codes_for_request"]
    seen: set[str] = set()
    modules = {"takealot_ops.erp.radar_materialized", "takealot_ops.erp.live_updates",
               "takealot_ops.erp.radar_warmup", "takealot_ops.erp.radar_code_version"}
    digest = hashlib.sha256(b"materialized-radar-code-v1")
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in imports:
            modules.add(imports[name])
        symbol_node = symbols.get(name)
        if symbol_node is not None:
            digest.update(ast.dump(symbol_node, include_attributes=False).encode())
            pending.extend(sorted({n.id for n in ast.walk(symbol_node) if isinstance(n, ast.Name)}))

    # Imported project modules are conservatively hashed in full, including their
    # transitive imports. An added dependency automatically joins the fingerprint.
    visited: set[str] = set()
    while modules:
        module = min(modules)
        modules.remove(module)
        if module in visited or not module.startswith("takealot_ops"):
            continue
        visited.add(module)
        path = source.joinpath(*module.split(".")).with_suffix(".py")
        package = module.rpartition(".")[0]
        if not path.is_file():
            path = source.joinpath(*module.split("."), "__init__.py")
            package = module
        if not path.is_file():
            raise FileNotFoundError(f"Radar code dependency is missing: {module}")
        content = path.read_bytes()
        digest.update(module.encode())
        digest.update(content)
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_module = node.module or ""
                if node.level:
                    imported_module = importlib.util.resolve_name(
                        "." * node.level + imported_module, package)
                if imported_module != "takealot_ops.erp.web":
                    modules.add(imported_module)
                    for alias in node.names:
                        child = imported_module + "." + alias.name
                        if source.joinpath(*child.split(".")).with_suffix(".py").is_file():
                            modules.add(child)
    return digest.hexdigest()

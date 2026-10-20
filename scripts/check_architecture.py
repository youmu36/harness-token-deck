"""Enforce the Python dependency boundaries without importing application code."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def within(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def imports(tree: ast.AST, module: str, is_package: bool) -> list[tuple[int, str, bool]]:
    result: list[tuple[int, str, bool]] = []
    package = module.split(".") if is_package else module.split(".")[:-1]

    def visit(node: ast.AST, type_only: bool = False) -> None:
        if isinstance(node, ast.If):
            guard = ast.unparse(node.test)
            for child in node.body:
                visit(child, type_only or guard in {"TYPE_CHECKING", "typing.TYPE_CHECKING"})
            for child in node.orelse:
                visit(child, type_only)
            return
        if isinstance(node, ast.Import):
            result.extend((node.lineno, alias.name, type_only) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(package[: len(package) - node.level + 1] + ([base] if base else []))
            result.extend((node.lineno, f"{base}.{alias.name}", type_only) for alias in node.names)
        for child in ast.iter_child_nodes(node):
            visit(child, type_only)

    visit(tree)
    return result


def violation(source: str, target: str, type_only: bool) -> str | None:
    top = target.split(".")[0]
    if within(target, "htu.migration") and not any(
        within(source, prefix) for prefix in ("htu.migration", "htu.bootstrap", "htu.__main__", "htu.tools")
    ):
        return "migration may only be imported by bootstrap or CLI modules"
    restricted = {
        "sqlite3": ("htu.adapters.sqlite", "htu.migration.legacy"),
        "keyring": ("htu.adapters.credentials",),
        "webview": ("htu.adapters.desktop",),
        "clr": ("htu.adapters.desktop",),
    }
    if top in restricted and not any(within(source, prefix) for prefix in restricted[top]):
        return f"{top} is restricted to its adapter"
    if within(source, "htu.domain"):
        if top in {"os", "pathlib", "threading", "sqlite3"}:
            return "domain must not depend on I/O or threading"
        if top not in sys.stdlib_module_names and not within(target, "htu.domain"):
            return "domain only imports the standard library and domain"
        own_domain = source.split(".")[2:3]
        other_domain = target.split(".")[2:3]
        if own_domain != other_domain and any(part.startswith("_") for part in target.split(".")[3:]):
            return "cross-domain private import"
    if (
        within(source, "htu.application")
        and top not in sys.stdlib_module_names
        and not any(within(target, prefix) for prefix in ("htu.domain", "htu.application"))
    ):
        return "application must depend on ports, not implementations"
    if within(source, "htu.adapters.sqlite"):
        if within(target, "htu.ipc") or (
            within(target, "htu.adapters") and not within(target, "htu.adapters.sqlite")
        ):
            return "SQLite adapter cannot import IPC or other adapters"
        if within(target, "htu.application") and not within(target, "htu.application.ports"):
            return "SQLite adapter may only import application ports"
    if within(source, "htu.adapters.desktop") and within(target, "htu.domain"):
        return "desktop must not depend on domain rules"
    if within(source, "htu.ipc") and within(target, "htu.adapters") and not type_only:
        return "IPC adapter imports are only allowed under TYPE_CHECKING"
    return None


def has_float(annotation: ast.AST) -> bool:
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == "float":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "float":
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                if has_float(ast.parse(node.value, mode="eval")):
                    return True
            except SyntaxError:
                continue
    return False


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        parts = list(relative.with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        module = ".".join(["htu", *(parts[:-1] if is_package else parts)])
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            errors.append(f"{relative}:{error.lineno}: {error.msg}")
            continue
        for line, target, type_only in imports(tree, module, is_package):
            reason = violation(module, target, type_only)
            if reason:
                errors.append(f"{relative}:{line}: {target}: {reason}")
        if any(part in {"money", "billing", "wallet"} for part in parts):
            for node in ast.walk(tree):
                annotation: ast.AST | None = None
                if isinstance(node, (ast.AnnAssign, ast.arg)):
                    annotation = node.annotation
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    annotation = node.returns
                if annotation is not None and has_float(annotation):
                    errors.append(f"{relative}:{annotation.lineno}: float annotation in monetary module")
    return errors


def main() -> int:
    errors = check(ROOT / "backend/src/htu")
    for error in errors:
        print(error)
    print(f"Python architecture: {len(errors)} violation(s)")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())

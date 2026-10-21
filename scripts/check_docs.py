"""Validate documentation paths, heading anchors and navigation without network access."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+\"[^\"]*\")?\)")


def without_fences(content: str) -> str:
    return re.sub(r"(?m)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1\s*$", "", content, flags=re.DOTALL)


def heading_anchors(content: str) -> set[str]:
    content = without_fences(content)
    anchors: set[str] = set()
    for match in re.finditer(r"(?m)^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", content):
        title = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", match[1])
        title = re.sub(r"<[^>]+>", "", title).strip().lower()
        slug = re.sub(r"[^\w -]", "", title).replace(" ", "-")
        anchor = slug
        suffix = 0
        while anchor in anchors:
            suffix += 1
            anchor = f"{slug}-{suffix}"
        anchors.add(anchor)
    anchors.update(re.findall(r"<(?:a|h[1-6])\b[^>]*\b(?:id|name)=[\"\']([^\"\']+)", content))
    return anchors


def check(root: Path) -> tuple[list[str], list[str]]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    documents = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
    documents += [path for path in (root / "AGENTS.md", root / "scripts/probes/README.md") if path.exists()]
    documents += [
        path
        for path in (
            root / "CONTRIBUTING.md",
            root / "SECURITY.md",
            root / "fixtures/README.md",
            root / "fixtures/legacy/README.md",
            root / "fixtures/remote/newapi/README.md",
            root / "packaging/licenses/README.md",
        )
        if path.exists()
    ]
    navigation: dict[Path, set[Path]] = {}
    anchors: dict[Path, set[str]] = {}
    for path in documents:
        relative = path.relative_to(root).as_posix()
        if not path.exists():
            errors.append(f"Missing document: {relative}")
            continue
        content = path.read_text(encoding="utf-8")
        if len(content.splitlines()) > 200:
            warnings.append(f"{relative} exceeds 200 lines")
        content = without_fences(content)
        content = re.sub(r"(`+).*?\1(?!`)", "", content, flags=re.DOTALL)
        navigation[path] = set()
        for match in LINK.finditer(content):
            target = match[1].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            destination = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
            if not destination.exists():
                errors.append(f"{relative}: missing link {target}")
                continue
            if destination.suffix.lower() == ".md" and destination.is_file():
                navigation[path].add(destination)
                if parsed.fragment:
                    if destination not in anchors:
                        anchors[destination] = heading_anchors(destination.read_text(encoding="utf-8"))
                    if unquote(parsed.fragment) not in anchors[destination]:
                        errors.append(f"{relative}: missing anchor {target}")
    entry = root / "docs/README.md"
    if entry.exists():
        reached: set[Path] = set()
        pending = [entry]
        while pending:
            path = pending.pop()
            if path not in reached:
                reached.add(path)
                pending.extend(navigation.get(path, set()) - reached)
        for path in sorted((root / "docs").rglob("*.md")):
            if path not in reached:
                warnings.append(f"{path.relative_to(root).as_posix()} is not reachable from docs/README.md")
    return errors, warnings


def main() -> int:
    errors, warnings = check(ROOT)
    for warning in warnings:
        print(f"WARN {warning}")
    for error in errors:
        print(f"FAIL {error}")
    print(f"Documentation: {len(errors)} broken link(s), {len(warnings)} warning(s)")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())

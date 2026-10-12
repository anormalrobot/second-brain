#!/usr/bin/env python3
"""Check this vault's local links and structure using only the standard library.

This is a checker for the documented full-path convention, not an Obsidian
parser. It checks file targets, not heading/block anchors or external URLs.
"""

from datetime import date
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote


WIKILINK = re.compile(r"(?<!!)\[\[([^\]\n]+)\]\]|!\[\[([^\]\n]+)\]\]")
MARKDOWN_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def link_prefix(root):
    """A managed brain can occupy one visible folder in an existing Obsidian vault."""
    marker = Path(root) / '.second-brain.json'
    if not marker.exists():
        return ''
    config = json.loads(marker.read_text(encoding='utf-8'))
    prefix = config.get('link_prefix', '')
    if not isinstance(prefix, str) or (prefix and (
            prefix in {'.', '..'} or prefix.startswith('.') or
            any(c in prefix for c in '/\\[]|#\n\r'))):
        raise ValueError('Invalid managed folder in .second-brain.json')
    if prefix and Path(root).name != prefix:
        raise ValueError(f'Managed brain must remain in a folder named {prefix!r}; open its parent as the Obsidian vault')
    return prefix


def wiki_target(root, relative):
    target = relative[:-3] if relative.endswith('.md') else relative
    prefix = link_prefix(root)
    return prefix + '/' + target if prefix else target


def wiki_relative(root, target):
    prefix = link_prefix(root)
    if prefix:
        if not target.startswith(prefix + '/'):
            raise ValueError('Managed memory pointer is outside the brain folder')
        return target[len(prefix) + 1:]
    return target


def without_examples(text):
    """Ignore fenced and inline code, retaining line breaks for diagnostics."""
    result = []
    fence = None
    for line in text.splitlines(keepends=True):
        if fence:
            closing = r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$"
            if re.fullmatch(closing, line.rstrip("\n")):
                fence = None
            result.append("\n")
            continue
        opening = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\n"))
        if opening:
            fence = opening.group(1)
            result.append("\n")
            continue
        result.append(re.sub(r"(`+).*?\1", "", line))
    return "".join(result)


def check(root):
    root = Path(root).resolve()
    errors = []
    links = 0
    try:
        vault = root.parent if link_prefix(root) else root
    except (ValueError, OSError) as exc:
        return [str(exc)], 0, 0
    notes = sorted(
        p for p in root.rglob("*.md")
        if not any(part.startswith(".") for part in p.relative_to(root).parts)
        and p.relative_to(root).parts[0] != "raw"
    )
    edges = {p: set() for p in notes}

    def target(source, text, is_wiki, line):
        nonlocal links
        if is_wiki:
            text = text.replace("\\|", "|").split("|", 1)[0]
        elif SCHEME.match(text):
            return
        path = text.split("#", 1)[0].strip()
        if not is_wiki:
            path = unquote(path)
        if not path:
            destination = source
        else:
            if is_wiki and ".." in Path(path).parts:
                errors.append(f"{source.relative_to(root)}:{line}: wikilink must be vault-relative: {text}")
                return
            if is_wiki and not Path(path).suffix:
                path += ".md"
            destination = ((vault if is_wiki else source.parent) / path).resolve()
        links += 1
        if not destination.is_relative_to(vault):
            errors.append(f"{source.relative_to(root)}:{line}: target escapes vault: {text}")
        elif not destination.is_file():
            errors.append(f"{source.relative_to(root)}:{line}: missing target: {text}")
        else:
            edges[source].add(destination)

    for note in notes:
        original = note.read_text(encoding="utf-8")
        if note.relative_to(root).parts[0] in {"memory", "wiki"}:
            frontmatter = re.match(r"\A---\n(.*?)\n---(?:\n|$)", original, re.S)
            fields = dict(re.findall(r"^([a-z_]+):[ \t]*([^\n]*)$", frontmatter.group(1), re.M)) if frontmatter else {}
            for key in ("created", "updated", "status"):
                if not fields.get(key, "").strip():
                    errors.append(f"{note.relative_to(root)}: missing metadata: {key}")
            for key in ("created", "updated", "last_verified"):
                if key in fields:
                    try:
                        date.fromisoformat(fields[key])
                    except ValueError:
                        errors.append(f"{note.relative_to(root)}: invalid ISO date: {key}")
            if fields.get("created", "") > fields.get("updated", "~"):
                errors.append(f"{note.relative_to(root)}: updated precedes created")
        body = without_examples(original)
        for match in WIKILINK.finditer(body):
            target(note, match.group(1) or match.group(2), True, body.count("\n", 0, match.start()) + 1)
        for match in MARKDOWN_LINK.finditer(body):
            target(note, match.group(1), False, body.count("\n", 0, match.start()) + 1)

    claude = root / "CLAUDE.md"
    agents = root / "AGENTS.md"
    if not claude.is_file() or claude.read_text(encoding="utf-8").strip() != "@AGENTS.md":
        errors.append("CLAUDE.md: expected the shared @AGENTS.md import")
    if not agents.is_file() or not agents.read_text(encoding="utf-8").strip():
        errors.append("AGENTS.md: shared instructions are missing or empty")
    edges.setdefault(claude, set()).add(agents)

    index = root / "index.md"
    if not index.is_file():
        errors.append("index.md: memory map is missing")
    visited = set()
    pending = [index]
    while pending:
        node = pending.pop()
        if node not in visited:
            visited.add(node)
            pending.extend(edges.get(node, ()))
    for note in notes:
        if note not in visited:
            errors.append(f"{note.relative_to(root)}: unreachable from index.md")
    return errors, len(notes), links


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    errors, notes, links = check(root)
    if errors:
        print("FAIL\n" + "\n".join(errors))
        return 1
    print(f"PASS: {notes} maintained Markdown files, {links} local links; metadata, reachability, and shared import checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

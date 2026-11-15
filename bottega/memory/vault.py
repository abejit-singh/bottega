"""Company memory: a persistent, Obsidian-compatible vault of Markdown notes.

Two kinds of memory exist in Bottega:
  * project memory   -> output/<project_id>/  (one job's deliverables, thrown away after)
  * company memory   -> memory/               (survives across projects: this module)

Design decisions:
  * Plain Markdown files, not a database. Humans can open the vault in Obsidian
    (graph view, backlinks) and agents can read it through tools: one source of
    truth for both.
  * Each note starts with YAML "frontmatter" (author, date, tags). Obsidian
    understands it natively, and we use it for search.
  * Notes link to each other with [[Note title]] wikilinks. Obsidian resolves a
    link by file name, so the file name IS the title.
  * Search is simple keyword scoring. No embeddings or extra dependencies, easy
    to understand and debug, good enough for hundreds of notes. Semantic search
    with embeddings is a natural future upgrade behind the same `search()` API.
  * All paths go through the same `Workspace` sandbox as the file tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from bottega.sandbox import SandboxError, Workspace

# Folders agents may save into. A fixed set keeps the vault organized.
FOLDERS = ("clients", "playbooks", "lessons", "projects")

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_TAGS_LINE = re.compile(r"^tags:\s*\[(.*)\]\s*$", re.MULTILINE)
# Characters that are illegal in file names or have special meaning in Obsidian links.
_BAD_TITLE_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
_WORD = re.compile(r"[a-zà-ÿ0-9]+")


@dataclass(frozen=True)
class NoteInfo:
    """What the dashboard shows for a note without opening it."""

    path: str
    title: str
    folder: str  # "" for notes at the top level of the vault
    tags: tuple[str, ...]
    author: str
    updated: str


@dataclass(frozen=True)
class SearchHit:
    path: str
    title: str
    tags: tuple[str, ...]
    score: int
    snippet: str


def _clean_title(title: str) -> str:
    cleaned = _BAD_TITLE_CHARS.sub("", title).strip().strip(".")
    if not cleaned:
        raise SandboxError("Note title is empty after removing special characters.")
    return cleaned[:100]


def _split_frontmatter(text: str) -> tuple[str, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end():]


def _parse_field(frontmatter: str, name: str) -> str:
    match = re.search(rf"^{name}:\s*(.+?)\s*$", frontmatter, re.MULTILINE)
    return match.group(1) if match else ""


def _parse_tags(frontmatter: str) -> tuple[str, ...]:
    match = _TAGS_LINE.search(frontmatter)
    if not match:
        return ()
    return tuple(t.strip().strip("\"'").lower() for t in match.group(1).split(",") if t.strip())


class MemoryVault:
    def __init__(self, root: Path) -> None:
        self.workspace = Workspace(root)

    @property
    def root(self) -> Path:
        return self.workspace.root

    def list_notes(self) -> list[str]:
        """All notes, ignoring Obsidian's own config folder."""
        return [p for p in self.workspace.list_files()
                if p.endswith(".md") and not p.startswith(".obsidian/")]

    def notes(self) -> list[NoteInfo]:
        """Metadata for every note, read from each file's frontmatter."""
        infos = []
        for rel in self.list_notes():
            frontmatter, _ = _split_frontmatter(self.workspace.read_text(rel))
            path = Path(rel)
            infos.append(NoteInfo(
                path=rel,
                title=path.stem,
                folder=path.parent.as_posix() if path.parent.as_posix() != "." else "",
                tags=_parse_tags(frontmatter),
                author=_parse_field(frontmatter, "author"),
                updated=_parse_field(frontmatter, "updated"),
            ))
        return infos

    def read(self, note: str) -> str:
        """Read a note by relative path ('playbooks/Landing page checklist.md')
        or just by title ('Landing page checklist'), like an Obsidian link."""
        path = note if note.endswith(".md") else self._find_by_title(note)
        return self.workspace.read_text(path)

    def save(self, *, folder: str, title: str, content: str,
             tags: list[str], author: str, today: date | None = None) -> str:
        """Create or overwrite a note. Returns its relative path."""
        if folder not in FOLDERS:
            raise SandboxError(f"Folder must be one of {', '.join(FOLDERS)}.")
        clean = _clean_title(title)
        tag_list = ", ".join(sorted({t.strip().lower().replace(" ", "-") for t in tags if t.strip()}))
        body = _split_frontmatter(content)[1].strip()  # drop any frontmatter the agent wrote
        note = (
            f"---\nauthor: {author}\nupdated: {(today or date.today()).isoformat()}\n"
            f"tags: [{tag_list}]\n---\n\n# {clean}\n\n{body}\n"
        )
        rel = f"{folder}/{clean}.md"
        self.workspace.write_text(rel, note)
        return rel

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        """Rank notes by keyword matches: title (x5) > tags (x3) > body (x1)."""
        terms = {w for w in _WORD.findall(query.lower()) if len(w) > 2}
        if not terms:
            return []
        hits: list[SearchHit] = []
        for rel in self.list_notes():
            text = self.workspace.read_text(rel)
            frontmatter, body = _split_frontmatter(text)
            title = Path(rel).stem
            tags = _parse_tags(frontmatter)
            title_words = set(_WORD.findall(title.lower()))
            tag_words = {w for t in tags for w in _WORD.findall(t)}
            body_lower = body.lower()
            score = sum(
                5 * (t in title_words) + 3 * (t in tag_words) + body_lower.count(t)
                for t in terms
            )
            if score > 0:
                hits.append(SearchHit(rel, title, tags, score, _snippet(body, terms)))
        hits.sort(key=lambda h: (-h.score, h.path))
        return hits[:limit]

    def _find_by_title(self, title: str) -> str:
        wanted = title.strip().lower()
        for rel in self.list_notes():
            if Path(rel).stem.lower() == wanted:
                return rel
        raise SandboxError(f"No note titled {title!r}. Use search_memory to find notes.")


def _snippet(body: str, terms: set[str], width: int = 200) -> str:
    """A short excerpt around the first matching term."""
    lower = body.lower()
    positions = [lower.find(t) for t in terms if t in lower]
    start = max(0, min(positions) - 60) if positions else 0
    excerpt = " ".join(body[start:start + width].split())
    return ("…" if start > 0 else "") + excerpt + ("…" if start + width < len(body) else "")

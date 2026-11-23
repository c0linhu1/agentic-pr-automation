"""Prepares a PR diff for the prompt: one piece per file, packed into parts that fit the context window
(big files split at hunks). Which files are left out is skips.py."""
import re
from dataclasses import dataclass


@dataclass
class FileDiff:
    path: str
    text: str
    context: str = ""  # the file's full contents after the change, when available (PR reviews)

    @property
    def size(self) -> int:
        """Characters this piece takes in a prompt, including its context block."""
        return len(self.text) + 1 + (len(self.context) + len(self.path) + 20 if self.context else 0)


GIT_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)
PLAIN_FILE = re.compile(r"^--- .*\n\+\+\+ ", re.M)
PLUS_PATH = re.compile(r"^\+\+\+ (?:b/)?(\S+)", re.M)
MINUS_PATH = re.compile(r"^--- (?:a/)?(\S+)", re.M)
HUNK = re.compile(r"^@@", re.M)


def split_files(diff: str) -> list[FileDiff]:
    """One FileDiff per file. Handles git diffs and pasted `---`/`+++` diffs."""
    matches = list(GIT_FILE.finditer(diff)) or list(PLAIN_FILE.finditer(diff))
    if not matches:
        return [FileDiff("(diff)", diff.strip())] if diff.strip() else []
    starts = [m.start() for m in matches] + [len(diff)]
    files = []
    for i, (a, b) in enumerate(zip(starts, starts[1:])):
        text = diff[a:b].rstrip("\n")
        if i == 0 and diff[:a].strip():          # text before the first file header
            text = diff[:a].rstrip("\n") + "\n" + text
        files.append(FileDiff(_path_of(text), text))
    return files


def _path_of(text: str) -> str:
    """The file's path after the change; its old path if it was deleted."""
    for pattern in (PLUS_PATH, MINUS_PATH):
        m = pattern.search(text)
        if m and m.group(1) != "/dev/null":
            return m.group(1)
    git = GIT_FILE.search(text)
    return git.group(2) if git else "(unknown file)"


def _split_big_file(f: FileDiff, budget: int) -> list[FileDiff]:
    """Split one file's diff at hunk boundaries; a single oversized hunk is cut by lines."""
    first = HUNK.search(f.text)
    if not first:
        return _cut_lines(f.path, "", f.text, budget)
    header, body = f.text[:first.start()], f.text[first.start():]
    hunk_starts = [m.start() for m in HUNK.finditer(body)] + [len(body)]
    hunks = [body[a:b] for a, b in zip(hunk_starts, hunk_starts[1:])]

    pieces, current = [], ""
    room = max(budget - len(header), 1000)
    for h in hunks:
        if len(h) > room:
            if current:
                pieces.append(current)
                current = ""
            pieces += [p.text[len(header):] for p in _cut_lines(f.path, header, h, budget)]
            continue
        if current and len(current) + len(h) > room:
            pieces.append(current)
            current = ""
        current += h
    if current:
        pieces.append(current)
    n = len(pieces)
    return [FileDiff(f"{f.path} (piece {i}/{n})", header + p) for i, p in enumerate(pieces, 1)]


def _cut_lines(path: str, header: str, text: str, budget: int) -> list[FileDiff]:
    """The last resort for one hunk too big for a part: cut it between lines (a line longer than a whole
    part is truncated), each piece marked as continuing in the next."""
    room = max(budget - len(header) - 60, 500)
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > room:
            chunks.append(current)
            current = ""
        current += line[:room]
    if current:
        chunks.append(current)
    n = len(chunks)
    return [FileDiff(f"{path} (piece {i}/{n})",
                     header + c + ("" if i == n else "\n... (continues in the next piece)\n"))
            for i, c in enumerate(chunks, 1)]


def pack(files: list[FileDiff], budget: int) -> list[list[FileDiff]]:
    """Group files into parts of at most `budget` characters, in order. A file that doesn't fit
    loses its context first, then is split at hunks."""
    parts, current, size = [], [], 0
    for f in files:
        if f.size <= budget:
            pieces = [f]
        elif len(f.text) + 1 <= budget:
            pieces = [FileDiff(f.path, f.text)]  # fits without its context
        else:
            pieces = _split_big_file(FileDiff(f.path, f.text), budget)
        for piece in pieces:
            if current and size + piece.size > budget:
                parts.append(current)
                current, size = [], 0
            current.append(piece)
            size += piece.size
    if current:
        parts.append(current)
    return parts


def pack_with_context(files: list[FileDiff], contents: dict[str, str], budget: int) -> list[list[FileDiff]]:
    """Like pack(), with each file's full contents attached only while that adds no parts.
    The largest files lose their context first."""
    baseline = len(pack(files, budget))
    with_context = [FileDiff(f.path, f.text, contents.get(f.path, "")) for f in files]
    parts = pack(with_context, budget)
    for f in sorted((f for f in with_context if f.context), key=lambda f: len(f.context), reverse=True):
        if len(parts) <= baseline:
            break
        f.context = ""
        parts = pack(with_context, budget)
    return parts

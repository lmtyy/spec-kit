"""Exact ownership verification for generated agent artifacts.

Registration stamps every generated file with a marker naming its owner.  Cleanup
may delete such a file only while that marker is still present *verbatim in the
place the writer put it*.  A substring hit anywhere else -- a mention inside the
user's own body, or a longer identifier that merely shares a prefix -- is not
ownership, and deleting on it would destroy work the extension or preset does not
own.

Writers and the markers they emit (each verified against the emitting code):

* extension Markdown -- ``<!-- Extension: <id> -->`` (plus ``<!-- Config: ... -->``)
* Markdown / TOML / Goose YAML -- ``<!-- Source: <id> -->`` or ``# Source: <id>``
* native-agent command skill -- frontmatter ``source: <id>:<file>``
* extension skill mirror -- frontmatter ``metadata.source: extension:<id>``
* preset command skill / mirror -- frontmatter ``metadata.source: preset:<id>``
* project-override skill -- frontmatter ``metadata.source: override:<name>``
  (owned by the project, so never matched for an extension or preset)

The ``extension``/``preset`` kinds are deliberately distinct: a preset must not be
able to claim an extension's artifact, or the reverse, even when the identifiers
are identical.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
from pathlib import Path
from typing import Sequence

import yaml

# A depth counter, not a flag: overlapping (not just nested) uses must not turn the
# exemption off for each other, and a counter needs no token to reset.
_TRANSACTIONAL_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "specify_transactional_artifact_depth", default=0
)


class _TransactionalWrites(contextlib.ContextDecorator):
    """Mark writes a transaction restores exactly on failure.

    An install (and its rollback) legitimately writes over a destination that is not
    verified managed output -- that is what installing means -- and the transaction
    guarantees the previous bytes come back if it fails.  Everywhere else a write no
    owner can be verified for is refused instead.
    """

    def _recreate_cm(self):
        # Each decorated invocation shares the counter rather than holding its own
        # field, so nesting and overlap both behave.
        return type(self)()

    def __enter__(self):
        _TRANSACTIONAL_DEPTH.set(_TRANSACTIONAL_DEPTH.get() + 1)
        return self

    def __exit__(self, *exc_info):
        _TRANSACTIONAL_DEPTH.set(max(0, _TRANSACTIONAL_DEPTH.get() - 1))
        return False


transactional_artifact_writes = _TransactionalWrites()


def transactional_writes_active() -> bool:
    return _TRANSACTIONAL_DEPTH.get() > 0


#: Paths this process's own writers generated.  A layer file (an integration
#: scaffold, for instance) carries no ownership marker, yet registration must be
#: able to take it over; the writer that created it says so here instead.
_GENERATED_THIS_OPERATION: contextvars.ContextVar[set | None] = contextvars.ContextVar(
    "specify_generated_this_operation", default=None
)


def note_generated_write(path) -> None:
    """Record that our own machinery generated *path* during this process."""
    current = _GENERATED_THIS_OPERATION.get()
    if current is None:
        current = set()
        _GENERATED_THIS_OPERATION.set(current)
    try:
        current.add(str(Path(path)))
    except (TypeError, ValueError):
        pass


def generated_this_operation(path) -> bool:
    """True when our own machinery generated *path* earlier in this process."""
    current = _GENERATED_THIS_OPERATION.get()
    if not current:
        return False
    try:
        return str(Path(path)) in current
    except (TypeError, ValueError):
        return False


__all__ = [
    "OWNER_KINDS",
    "artifact_belongs_to",
    "artifact_file_belongs_to",
    "header_region",
    "is_managed_output",
    "transactional_artifact_writes",
    "transactional_writes_active",
    "note_generated_write",
    "generated_this_operation",
]

_SCHEMES = ("extension", "preset", "override")
_MANAGED_COMMENT_MARKERS = ("<!-- Extension: ", "<!-- Config: ", "<!-- Source: ", "# Source: ")
_MANAGED_VALUE_SCHEMES = (*_SCHEMES,)


def is_managed_output(text: str, style: str = "markdown") -> bool:
    """True when *text* carries any recognised ownership marker.

    Registration may overwrite its own previous output, another owner's output, or the
    extension/preset state; it may not silently overwrite a file that no owner claims.
    """
    frontmatter, pre_body = _split_header(text, style)
    emitted = _comment_lines(pre_body)
    if style == "yaml":
        emitted |= _footer_comment_lines(text)
    for line in emitted:
        for marker in _MANAGED_COMMENT_MARKERS:
            if line.startswith(marker):
                return True
    for value in _frontmatter_values("\n".join(frontmatter)):
        # Any ``source`` in this project's vocabulary is an ownership claim: a reserved
        # scheme (``extension:``/``preset:``/``override:``) or the bare ``<id>:<file>``
        # composite the native-skill writers emit.  A value with no separator is prose.
        scheme, separator, rest = value.partition(":")
        if separator and scheme and rest:
            return True
        # The core/template layer stamps its renders with a repo-relative path
        # (``templates/commands/<name>.md``) rather than a scheme: still a claim.
        if not separator and value.endswith(".md") and "/" in value:
            return True
    return False


OWNER_KINDS = ("extension", "preset")

_FRONTMATTER_DELIMITER = "---"
_COMMENT_PREFIXES = ("<!--", "#", "//", ";")


_BLOCK_SCALAR = ('"""', "'''")
# A YAML/TOML header assignment that opens a block scalar (``prompt: |2``): everything
# after it is content, so a marker-shaped line inside it is prose, not provenance.
_OPENS_BODY = re.compile(r"^[A-Za-z_][\w.\-]*\s*[:=]\s*[|>]")
_KEY_VALUE = re.compile(r"^[A-Za-z_][\w.\-]*\s*[:=]")


def header_region(text: str, style: str = "markdown") -> str:
    """Return the region of *text* that may carry an ownership marker.

    That is the frontmatter block (if any) plus the generated header the
    renderer writes before the body:

    * ``markdown`` -- the run of ``<!-- ... -->`` comments, the only comment
      form the Markdown writers emit.  A ``#`` line there is a Markdown
      heading, i.e. body, so the region ends before it.
    * ``toml`` / ``yaml`` -- comment lines and ``key = value`` / ``key: value``
      header lines, ending at the first block scalar that opens the body.

    Everything past the region is artifact content, where a marker-shaped line
    is prose rather than provenance.
    """
    frontmatter, pre_body = _split_header(text, style)
    return "\n".join(frontmatter + pre_body)


def _split_header(text: str, style: str) -> tuple[list[str], list[str]]:
    """Return (frontmatter block, generated comment header) for *text*.

    Comments are read only from the second part.  A frontmatter scalar can contain
    marker-shaped text (``description: |`` with the marker on the next line) that is
    prose the user or another writer put there, not provenance.
    """
    lines = text.splitlines()
    frontmatter: list[str] = []
    index = 0
    if lines and lines[0].strip() == _FRONTMATTER_DELIMITER:
        frontmatter.append(lines[0])
        for index in range(1, len(lines)):
            frontmatter.append(lines[index])
            if lines[index].strip() == _FRONTMATTER_DELIMITER:
                index += 1
                break
    pre_body: list[str] = []
    for line in lines[index:]:
        stripped = line.strip()
        if style == "markdown":
            if stripped and not stripped.startswith("<!--"):
                break
            pre_body.append(line)
            continue
        if stripped.startswith(_BLOCK_SCALAR) or stripped.endswith(_BLOCK_SCALAR):
            break
        if _OPENS_BODY.match(stripped):
            break
        if not stripped or stripped.startswith(_COMMENT_PREFIXES) or _KEY_VALUE.match(stripped):
            pre_body.append(line)
            continue
        break
    return frontmatter, pre_body


def _footer_comment_lines(text: str) -> set[str]:
    """Trailing comment lines, where the Goose YAML writer stamps its source.

    The Goose renderer writes ``# Source: <id>`` *after* the prompt, so that single
    documented footer position is accepted too.  A non-comment line above it ends the
    scan, so a marker inside the recipe body is never provenance.
    """
    footer: list[str] = []
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            if footer:
                break
            continue
        if not stripped.startswith(_COMMENT_PREFIXES):
            break
        footer.append(line)
    return {line.strip() for line in footer}

def _frontmatter_values(header: str) -> set[str]:
    """Return ``source`` values declared in *header*, flat or nested, quoted or not."""
    block: list[str] = []
    lines = header.splitlines()
    if lines and lines[0].strip() == _FRONTMATTER_DELIMITER:
        for line in lines[1:]:
            if line.strip() == _FRONTMATTER_DELIMITER:
                break
            block.append(line)
    if not block:
        return set()
    try:
        parsed = yaml.safe_load("\n".join(block))
    except yaml.YAMLError:
        return set()
    if not isinstance(parsed, dict):
        return set()
    values: set[str] = set()
    for holder in (parsed, parsed.get("metadata")):
        if isinstance(holder, dict):
            source = holder.get("source")
            if isinstance(source, str):
                values.add(source.strip())
    return values


def _comment_lines(lines: Sequence[str]) -> set[str]:
    return {line.strip() for line in lines if line.strip().startswith(_COMMENT_PREFIXES)}


def _expected_forms(kind: str, owner_id: str) -> tuple[set[str], set[str]]:
    """Return (exact comment lines, frontmatter value predicates' prefixes)."""
    if kind not in OWNER_KINDS:
        raise ValueError(f"unknown owner kind: {kind!r}")
    if kind == "extension":
        comments = {
            f"<!-- Extension: {owner_id} -->",
            f"<!-- Config: .specify/extensions/{owner_id}/ -->",
            f"<!-- Source: {owner_id} -->",
            f"# Source: {owner_id}",
            f"<!-- Source: extension:{owner_id} -->",
            f"# Source: extension:{owner_id}",
        }
        values = {owner_id, f"extension:{owner_id}"}
    else:
        comments = {
            f"<!-- Source: {owner_id} -->",
            f"# Source: {owner_id}",
            f"<!-- Source: preset:{owner_id} -->",
            f"# Source: preset:{owner_id}",
        }
        values = {owner_id, f"preset:{owner_id}"}
    return comments, values


def _value_matches(value: str, prefixes: set[str], owner_id: str, kind: str) -> bool:
    """True when *value* names exactly this owner.

    A scheme prefix is decided first and never falls through to the composite form, so
    an extension whose id is literally ``preset`` (or ``override``) cannot claim
    ``preset:<other>`` / ``override:<command>``.  Otherwise ``<owner>:<file>``
    composites are accepted, anchored on the identifier and a following ``:``, so
    ``demo-extra`` never matches ``demo``.
    """
    scheme, separator, rest = value.partition(":")
    if scheme in _SCHEMES:
        if separator and scheme == "override":
            # Project overrides belong to the project, never to an extension/preset.
            return False
        return scheme == kind and rest == owner_id
    if scheme in _SCHEMES:
        return False
    if value in prefixes:
        return True
    return value.startswith(f"{owner_id}:")


def artifact_belongs_to(
    text: str, *, kind: str, owner_id: str, style: str = "markdown"
) -> bool:
    """True when *text* still declares *owner_id* as its owner, in marker position."""
    comments, values = _expected_forms(kind, owner_id)
    frontmatter, pre_body = _split_header(text, style)
    emitted = _comment_lines(pre_body)
    if style == "yaml":
        emitted |= _footer_comment_lines(text)
    if emitted & comments:
        return True
    header = "\n".join(frontmatter)
    return any(
        _value_matches(value, values, owner_id, kind)
        for value in _frontmatter_values(header)
    )


def artifact_file_belongs_to(
    path: Path,
    *,
    kind: str,
    owner_id: str,
    own_directories: Sequence[Path] = (),
    style: str = "markdown",
) -> bool:
    """True when *path* is still owned by *owner_id* on disk.

    A symlink is owned only when it resolves inside one of *own_directories* (the
    extension's or preset's own staging area).  Any other link -- the user's, or
    another owner's -- is not ours to delete.  Unreadable files are not deleted.
    """
    if path.is_symlink():
        if not own_directories:
            return False
        try:
            target = path.resolve()
        except OSError:
            return False
        for directory in own_directories:
            try:
                target.relative_to(directory.resolve())
            except (OSError, ValueError):
                continue
            try:
                target.read_bytes()
            except OSError:
                # A link into our area we cannot read is not ours to delete.
                return False
            return True
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return artifact_belongs_to(text, kind=kind, owner_id=owner_id, style=style)

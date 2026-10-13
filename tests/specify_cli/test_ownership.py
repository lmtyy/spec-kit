"""Ownership markers are matched exactly, in marker position, per owner kind.

A substring hit is not ownership: the marker has to be where the generating
writer puts it, name exactly this owner, and never be claimable by the other
kind (extension vs preset) or by a longer identifier that shares a prefix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from specify_cli.ownership import artifact_belongs_to, is_managed_output, artifact_file_belongs_to, header_region

EXT_MARKDOWN = (
    "---\n"
    "description: x\n"
    "---\n"
    "<!-- Extension: demo -->\n"
    "<!-- Config: .specify/extensions/demo/ -->\n"
    "# Body\n"
)
TOML = 'description = "x"\n# Source: demo\n\nprompt = """body"""\n'
NATIVE_SKILL = "---\nname: n\nmetadata:\n  source: demo:commands/hello.md\n---\n# Body\n"
EXTENSION_MIRROR = "---\nname: n\nmetadata:\n  source: extension:demo\n---\n# Body\n"
PRESET_SKILL = "---\nname: n\nmetadata:\n  source: preset:demo\n---\n# Body\n"
OVERRIDE_SKILL = "---\nname: n\nmetadata:\n  source: override:speckit.demo\n---\n# Body\n"
USER_REWRITE = (
    "---\n"
    "description: my own file\n"
    "---\n"
    "# my own heading\n"
    "<!-- Extension: demo -->\n"
)
PREFIX_ID = "---\ndescription: x\n---\n<!-- Extension: demo-extra -->\n"


@pytest.mark.parametrize(
    ("text", "kind", "style"),
    [
        (EXT_MARKDOWN, "extension", "markdown"),
        (TOML, "extension", "toml"),
        (NATIVE_SKILL, "extension", "markdown"),
        (EXTENSION_MIRROR, "extension", "markdown"),
        (PRESET_SKILL, "preset", "markdown"),
        ("<!-- Source: preset:demo -->\nbody\n", "preset", "markdown"),
        ("# Source: demo\n", "preset", "toml"),
    ],
)
def test_marker_recognises_the_generating_writer(text, kind, style):
    """Every writer's own marker form is recognised for its owner."""
    assert artifact_belongs_to(text, kind=kind, owner_id="demo", style=style)


def test_marker_outside_the_generated_header_is_not_ownership():
    """Text the user wrote below the header cannot claim the file."""
    assert not artifact_belongs_to(USER_REWRITE, kind="extension", owner_id="demo")


def test_longer_identifier_sharing_a_prefix_is_not_ownership():
    """``demo-extra`` is a different owner than ``demo``."""
    assert not artifact_belongs_to(PREFIX_ID, kind="extension", owner_id="demo")


def test_owner_kinds_are_not_interchangeable():
    """A preset cannot claim an extension artifact, or the reverse."""
    assert not artifact_belongs_to(EXT_MARKDOWN, kind="preset", owner_id="demo")
    assert not artifact_belongs_to(PRESET_SKILL, kind="extension", owner_id="demo")


def test_project_override_belongs_to_neither_owner():
    """An override is the project's own content."""
    assert not artifact_belongs_to(OVERRIDE_SKILL, kind="extension", owner_id="demo")
    assert not artifact_belongs_to(OVERRIDE_SKILL, kind="preset", owner_id="demo")


def test_quoted_frontmatter_values_are_parsed():
    """A quoted YAML scalar is the same owner as an unquoted one."""
    text = '---\nmetadata:\n  source: "demo:commands/x.md"\n---\nbody\n'
    assert artifact_belongs_to(text, kind="extension", owner_id="demo")


def test_header_region_stops_at_the_markdown_body():
    region = header_region(EXT_MARKDOWN, "markdown")
    assert "Extension: demo" in region
    assert "# Body" not in region


def test_symlink_is_owned_only_inside_the_owner_directories(tmp_path):
    """A link we did not create is never ours to delete."""
    own = tmp_path / "extension"
    own.mkdir()
    inside = own / "cmd.md"
    inside.write_text(EXT_MARKDOWN, encoding="utf-8")
    outside = tmp_path / "user.md"
    outside.write_text(EXT_MARKDOWN, encoding="utf-8")

    link_inside = tmp_path / "inside.md"
    link_inside.symlink_to(inside)
    link_outside = tmp_path / "outside.md"
    link_outside.symlink_to(outside)

    assert artifact_file_belongs_to(
        link_inside, kind="extension", owner_id="demo", own_directories=(own,)
    )
    assert not artifact_file_belongs_to(
        link_outside, kind="extension", owner_id="demo", own_directories=(own,)
    )
    assert not artifact_file_belongs_to(link_outside, kind="extension", owner_id="demo")


def test_missing_or_unreadable_file_is_not_owned(tmp_path):
    assert not artifact_file_belongs_to(
        tmp_path / "absent.md", kind="extension", owner_id="demo"
    )


def test_real_renderers_stamp_what_the_matcher_accepts():
    """Round trip: every writer's own output is recognised for its own owner.

    The marker forms come from the real renderers, not from hand-written samples, so
    a writer that changes its stamp fails here instead of silently orphaning output.
    """
    from specify_cli.agents import CommandRegistrar

    registrar = CommandRegistrar(Path("."))
    frontmatter = {"description": "demo"}

    markdown = registrar.render_markdown_command(frontmatter, "body\n", "demo")
    assert artifact_belongs_to(markdown, kind="extension", owner_id="demo")
    assert artifact_belongs_to(markdown, kind="preset", owner_id="demo")

    extension_markdown = registrar.render_markdown_command(
        frontmatter,
        "body\n",
        "demo",
        "\n<!-- Extension: demo -->\n<!-- Config: .specify/extensions/demo/ -->\n",
    )
    assert artifact_belongs_to(extension_markdown, kind="extension", owner_id="demo")

    toml = registrar.render_toml_command(frontmatter, "body\n", "demo")
    assert artifact_belongs_to(toml, kind="extension", owner_id="demo", style="toml")

    native_frontmatter = registrar.build_skill_frontmatter(
        "codex", "speckit-demo", "desc", "demo:commands/demo.md"
    )
    native = registrar.render_frontmatter(native_frontmatter) + "\nbody\n"
    assert artifact_belongs_to(native, kind="extension", owner_id="demo")


def test_goose_recipe_footer_marker_is_recognised():
    """Goose stamps its source *after* the prompt, so the footer counts too."""
    from specify_cli.integrations.base import YamlIntegration

    text = YamlIntegration._render_yaml("Demo", "demo", "body\n", "demo")
    assert "# Source: demo" in text
    assert artifact_belongs_to(text, kind="extension", owner_id="demo", style="yaml")


def test_scalar_prose_is_not_ownership():
    """A marker inside a frontmatter scalar is text, not provenance."""
    text = "---\ndescription: |\n  <!-- Extension: demo -->\n---\nbody\n"
    assert not artifact_belongs_to(text, kind="extension", owner_id="demo")


@pytest.mark.parametrize(
    ("value", "owner_id"),
    [
        ("preset:demo", "preset"),
        ("override:speckit.demo", "override"),
        ("extension:demo", "extension"),
    ],
)
def test_reserved_schemes_are_not_claimable_as_owner_ids(value, owner_id):
    """An id that collides with a reserved scheme must not claim that scheme's files."""
    text = f"---\nmetadata:\n  source: {value}\n---\n"
    assert not artifact_belongs_to(text, kind="extension", owner_id=owner_id)
    assert not artifact_belongs_to(text, kind="preset", owner_id=owner_id)



_ACCEPTED_MARKER_VALUES = [
    "templates/commands/specify.md",
    "templates/commands/my command.md",
    "preset:demo",
    "extension:demo",
    "override:cmd",
    "demo:commands/x.md",
    "sample-extension:commands/sample.md",
    "demo",
    "preset:demo:commands/demo.md",
    "demo:commands/hello.txt",
    "demo:commands/hello.MD",
    "demo:commands/my command.md",
]

_REJECTED_MARKER_VALUES = [
    "notes/user.md",
    "notes/user",
    "https://example.org/document",
    "../notes/user.md",
    "C:\\notes\\user.md",
    "/abs/user.md",
    "preset:demo:../user.md",
    "preset:demo:/absolute.md",
    "C:notes/user.md",
    "notes about markers",
]


@pytest.mark.parametrize("value", _ACCEPTED_MARKER_VALUES)
def test_writer_marker_values_are_recognised(value):
    """Every form the writers emit counts as a claim -- including spaces and any suffix.

    A recogniser narrower than the writers' own path policy blocks legitimate refreshes
    of output we generated.
    """
    assert is_managed_output(f"---\nsource: {value}\n---\n")


@pytest.mark.parametrize("value", _REJECTED_MARKER_VALUES)
def test_user_looking_values_are_not_recognised(value):
    """A URL, an absolute or drive-relative path, a traversal, or prose is not a claim."""
    assert not is_managed_output(f"---\nsource: {value}\n---\n")




_YAML_SCALAR_CASES = [
    # An unindented marker inside a multi-line quoted scalar is the value's text.
    ('prompt: "first\n# Source: demo\nlast"\n', False),
    ("prompt: 'first\n# Source: demo\nlast'\n", False),
    # A genuine footer after a closed scalar is provenance.
    ('prompt: "hello"\n# Source: demo\n', True),
    ("prompt: 'hello'\n# Source: demo\n", True),
    # A block scalar keeps its indented marker as body text.
    ("prompt: |\n  user text\n  # Source: demo\n", False),
]


@pytest.mark.parametrize("text,expected", _YAML_SCALAR_CASES)
def test_yaml_scalar_text_is_not_provenance(text, expected):
    """A marker inside a YAML scalar is the value's text, not an ownership claim.

    Regression (review F3): a multi-line quoted ``prompt: "..."`` leaked its contents
    into the header region and the footer scan, so the marker inside it counted.
    """
    assert is_managed_output(text, "yaml") is expected

def test_comment_markers_are_recognised():
    """The comment forms the writers emit are recognised.

    OPEN (review F2, comment half): the *value* inside a comment marker is not validated
    yet, so `<!-- Source: https://example/a.md -->` is still accepted as a claim, and an
    unterminated `<!--` is rejected only by the comment-completeness check. Validating it
    needs the writers to emit a checkable owner token first: `source_id` is a free-form
    string today, and `CommandRegistrar` also emits a custom context note in that slot
    (`agents.py` context_note), so a stricter check rejects real output (56 tests).
    """
    assert is_managed_output("<!-- Source: provider -->\n")
    assert is_managed_output("<!-- Source: preset:demo -->\n")
    assert is_managed_output("<!-- Source: extension:demo -->\n")
    assert is_managed_output("<!-- Extension: demo -->\n")
    assert is_managed_output("<!-- Config: .specify/extensions/demo/ -->\n")
    assert not is_managed_output("<!-- Source: discussion about markers\n")

def test_operation_generated_output_is_takeable(tmp_path):
    """Our own machinery may take over a layer file it generated itself.

    The integration writer records what it wrote, so registration can refresh that
    file even though it carries no ownership marker -- while a file nobody recorded
    stays protected.
    """
    from specify_cli.ownership import generated_this_operation, note_generated_write

    written = tmp_path / "generated-command.md"
    assert not generated_this_operation(written)
    written.write_text("LAYER BODY\n", encoding="utf-8")
    note_generated_write(written)
    assert generated_this_operation(written)

    # Editing the file ends its provenance: it no longer holds the bytes we generated.
    written.write_text("MY OWN EDIT\n", encoding="utf-8")
    assert not generated_this_operation(written)
    assert not generated_this_operation(tmp_path / "somebody-elses.md")

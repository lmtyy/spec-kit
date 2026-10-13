"""Tests for ``specify extension disable``.

Mirrors ``specify_cli.extensions.command_disable``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from specify_cli import app
from specify_cli.extensions import (
    ExtensionManager,
    ExtensionRegistry,
    HookExecutor,
)


class TestExtensionDisableCLI:
    """CLI tests for ``specify extension disable``."""

    def test_disable_reenable_hint_escapes_extension_id_markup(self, tmp_path):
        """Disable success hints should not parse extension IDs as markup."""

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".specify").mkdir()

        extension_id = "[red]bad[/red]"
        installed = [
            {
                "id": extension_id,
                "name": "Bad Extension",
                "version": "1.0.0",
                "description": "Test extension",
                "enabled": True,
            }
        ]

        runner = CliRunner()
        with patch.object(Path, "cwd", return_value=project_dir), \
             patch.object(ExtensionManager, "list_installed", return_value=installed), \
             patch.object(ExtensionRegistry, "get", return_value={"enabled": True}), \
             patch.object(ExtensionRegistry, "update", return_value=None), \
             patch.object(HookExecutor, "get_project_config", return_value={}):
            result = runner.invoke(
                app,
                ["extension", "disable", extension_id],
                catch_exceptions=True,
            )

        assert result.exit_code == 0, result.output
        assert "specify extension enable [red]bad[/red]" in result.output


def _write_init_options(project_dir, agent, skills=True):
    """Persist the active integration the CLI resolves the agent from."""
    import json

    (project_dir / ".specify").mkdir(parents=True, exist_ok=True)
    (project_dir / ".specify" / "init-options.json").write_text(
        json.dumps({"ai": agent, "ai_skills": skills, "script": "sh"}), encoding="utf-8"
    )


def _install_for_agent(project_dir, extension_dir, agent, dev=True):
    """Install the fixture extension while *agent* is the active integration."""
    from specify_cli import app

    _write_init_options(project_dir, agent)
    runner = CliRunner()
    arguments = ["extension", "add", str(extension_dir)]
    if dev:
        arguments.append("--dev")
    with patch.object(Path, "cwd", return_value=project_dir):
        result = runner.invoke(app, arguments, catch_exceptions=True)
    assert result.exit_code == 0, result.output
    return result


def _registry_metadata(project_dir, extension_id="test-ext"):
    import json

    path = project_dir / ".specify" / "extensions" / ".registry"
    return json.loads(path.read_text(encoding="utf-8"))["extensions"][extension_id]


SKILL_NAME = "speckit-test-ext-hello"


def test_skill_only_extension_cleanup_after_agent_switch(extension_dir, project_dir):
    """A skills-only registration is cleaned up even after switching agents.

    ``copilot`` renders extension commands as ``SKILL.md`` files and records them in
    the agent-agnostic ``registered_skills`` list without ever appearing in
    ``registered_commands``. Disabling the extension after switching to ``codex``
    must still remove the ``SKILL.md`` from copilot's skills root while leaving the
    new agent's own files alone.
    """
    from specify_cli import app

    _install_for_agent(project_dir, extension_dir, "copilot")
    metadata = _registry_metadata(project_dir)
    assert metadata["registered_skills"] == [SKILL_NAME]
    assert metadata["registered_commands"] == {}
    owned = project_dir / ".github" / "skills" / SKILL_NAME / "SKILL.md"
    assert owned.is_file()
    assert "source: extension:test-ext" in owned.read_text(encoding="utf-8")

    # Switch the active integration, then let another agent own a same-named skill.
    _write_init_options(project_dir, "codex")
    foreign = project_dir / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text(
        "---\nmetadata:\n  source: extension:other-ext\n---\n", encoding="utf-8"
    )

    runner = CliRunner()
    with patch.object(Path, "cwd", return_value=project_dir):
        result = runner.invoke(app, ["extension", "disable", "test-ext"], catch_exceptions=True)
    assert result.exit_code == 0, result.output

    assert not owned.exists()
    assert not owned.parent.exists()
    assert foreign.is_file()
    assert "extension:other-ext" in foreign.read_text(encoding="utf-8")
    assert _registry_metadata(project_dir)["enabled"] is False


def test_disable_is_idempotent_and_keeps_unregistered_names(extension_dir, project_dir):
    """A repeated disable stays a no-op and never removes an unregistered skill."""
    from specify_cli import app

    _install_for_agent(project_dir, extension_dir, "copilot")
    unrelated = project_dir / ".github" / "skills" / f"{SKILL_NAME}-extra" / "SKILL.md"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("---\nmetadata:\n  source: user\n---\n", encoding="utf-8")

    runner = CliRunner()
    with patch.object(Path, "cwd", return_value=project_dir):
        first = runner.invoke(app, ["extension", "disable", "test-ext"], catch_exceptions=True)
        second = runner.invoke(app, ["extension", "disable", "test-ext"], catch_exceptions=True)
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already disabled" in " ".join(second.output.split())
    assert unrelated.is_file()
    assert not (project_dir / ".github" / "skills" / SKILL_NAME).exists()


def test_disable_tolerates_legacy_registry_without_provenance(extension_dir, project_dir):
    """Legacy metadata without provenance must not raise or over-delete."""
    import json

    from specify_cli import app

    _install_for_agent(project_dir, extension_dir, "copilot")
    registry_file = project_dir / ".specify" / "extensions" / ".registry"
    data = json.loads(registry_file.read_text(encoding="utf-8"))
    legacy = data["extensions"]["test-ext"]
    legacy.pop("registered_commands", None)
    legacy.pop("registered_skills", None)
    registry_file.write_text(json.dumps(data), encoding="utf-8")

    keeper = project_dir / ".github" / "skills" / "user-authored" / "SKILL.md"
    keeper.parent.mkdir(parents=True, exist_ok=True)
    keeper.write_text("---\nmetadata:\n  source: user\n---\n", encoding="utf-8")

    runner = CliRunner()
    with patch.object(Path, "cwd", return_value=project_dir):
        result = runner.invoke(app, ["extension", "disable", "test-ext"], catch_exceptions=True)
    assert result.exit_code == 0, result.output
    assert keeper.is_file()


def test_disable_removes_only_its_own_marker_when_agents_share_a_skill_name(
    extension_dir, project_dir
):
    """Another owner's same-named skill survives a disable."""
    from specify_cli import app

    _install_for_agent(project_dir, extension_dir, "copilot")
    shared = project_dir / ".github" / "skills" / SKILL_NAME / "SKILL.md"
    assert shared.is_file()
    shared.write_text("---\nmetadata:\n  source: preset:another-pack\n---\n", encoding="utf-8")

    runner = CliRunner()
    with patch.object(Path, "cwd", return_value=project_dir):
        result = runner.invoke(app, ["extension", "disable", "test-ext"], catch_exceptions=True)
    assert result.exit_code == 0, result.output
    assert shared.is_file()
    assert "preset:another-pack" in shared.read_text(encoding="utf-8")


def test_disable_preserves_a_user_rewritten_command_output(extension_dir, project_dir):
    """A command output the user rewrote is kept and reported, not deleted.

    Cleanup removes only files that still carry this extension's source marker;
    content the user has taken over is preserved so the operation cannot delete
    work it does not own.
    """
    from specify_cli import app

    _install_for_agent(project_dir, extension_dir, "claude")
    owned = project_dir / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
    assert owned.is_file(), sorted(p.name for p in (project_dir / ".claude" / "skills").iterdir())

    # The user takes the file over: the extension's link is replaced by their own
    # real file, which no longer carries this extension's source marker.
    owned.unlink()
    owned.write_text("# my own command\n", encoding="utf-8")
    assert "source: test-ext" not in owned.read_text(encoding="utf-8")

    runner = CliRunner()
    with patch.object(Path, "cwd", return_value=project_dir):
        result = runner.invoke(app, ["extension", "disable", "test-ext"], catch_exceptions=True)
    assert result.exit_code == 0, result.output
    assert owned.is_file()
    assert "my own command" in owned.read_text(encoding="utf-8")
    assert "no longer" in " ".join(result.output.split())


def _install_via_api(project_dir, extension_dir, agent, skills=False):
    """Install from a directory without dev links, as a released install would."""
    _write_init_options(project_dir, agent, skills=skills)
    manager = ExtensionManager(project_dir)
    manager.install_from_directory(extension_dir, "1.0.0", force=True, register_commands=True)
    return manager


def _files_containing(root, needle):
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and needle in path.read_text(encoding="utf-8", errors="replace")
    )


def _disable(project_dir):
    runner = CliRunner()
    with patch.object(Path, "cwd", return_value=project_dir):
        return runner.invoke(app, ["extension", "disable", "test-ext"], catch_exceptions=True)


def _bob_source(tmp_path):
    source = tmp_path / "ext-source"
    (source / "commands").mkdir(parents=True)
    (source / "commands" / "hello.md").write_text(
        '---\ndescription: "d"\n---\n\n# Body\n\n$ARGUMENTS\n', encoding="utf-8"
    )
    return source


def _register_marked_command(agent, source, project):
    """Write one extension-owned command through the real registrar."""
    from specify_cli.agents import CommandRegistrar

    registrar = CommandRegistrar(project)
    registrar.register_commands(
        agent,
        [{"name": "speckit.demo.hello", "file": "commands/hello.md", "description": "d"}],
        "demo",
        source,
        project,
        context_note="\n<!-- Extension: demo -->\n<!-- Config: .specify/extensions/demo/ -->\n",
        extension_id="demo",
    )
    return registrar


def _init_options(project, agent):
    import json

    (project / ".specify").mkdir(parents=True, exist_ok=True)
    (project / ".specify" / "init-options.json").write_text(
        json.dumps({"ai": agent, "ai_skills": False, "script": "sh"}), encoding="utf-8"
    )


def test_registrar_removes_its_own_legacy_extension_markdown(tmp_path):
    """``<!-- Extension: id -->`` is this extension's own marker, so it is removed.

    The extension Markdown writer emits that comment form; a cleanup that does not
    recognise it either leaves orphaned files behind or, worse, deletes files it
    cannot prove it owns.  Both sides are pinned here.
    """
    source = _bob_source(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _init_options(project, "bob")
    registrar = _register_marked_command("bob", source, project)
    owned = _files_containing(project, "<!-- Extension: demo -->")
    assert owned, "no marked command file was written"

    preserved = registrar.unregister_commands(
        {"bob": ["speckit.demo.hello"]},
        project,
        source_id="demo",
        owner_kind="extension",
        own_directories=(source,),
    )

    assert preserved == []
    for path in owned:
        assert not path.exists(), path


def test_registrar_keeps_a_file_another_owner_now_holds(tmp_path):
    """A marker naming ``demo-extra`` is a different owner's file."""
    source = _bob_source(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _init_options(project, "bob")
    registrar = _register_marked_command("bob", source, project)
    owned = _files_containing(project, "<!-- Extension: demo -->")
    assert owned
    taken = owned[0]
    # Another owner takes the file over: every marker naming ``demo`` is gone.
    taken.write_text(
        taken.read_text(encoding="utf-8")
        .replace("<!-- Extension: demo -->", "<!-- Extension: demo-extra -->")
        .replace(
            "<!-- Config: .specify/extensions/demo/ -->",
            "<!-- Config: .specify/extensions/demo-extra/ -->",
        ),
        encoding="utf-8",
    )

    preserved = registrar.unregister_commands(
        {"bob": ["speckit.demo.hello"]},
        project,
        source_id="demo",
        owner_kind="extension",
        own_directories=(source,),
    )

    assert preserved == [taken]
    assert "demo-extra" in taken.read_text(encoding="utf-8")


def test_registrar_keeps_a_rewritten_copilot_prompt(tmp_path):
    """A companion prompt is deleted only while it is still what we render."""
    source = _bob_source(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _init_options(project, "copilot")
    registrar = _register_marked_command("copilot", source, project)
    prompts = sorted(project.rglob("*.prompt.md"))
    assert prompts, "no companion prompt was written"
    prompt = prompts[0]
    prompt.write_text("---\nagent: mine\n---\nmy own prompt\n", encoding="utf-8")
    owned = _files_containing(project, "<!-- Extension: demo -->")

    preserved = registrar.unregister_commands(
        {"copilot": ["speckit.demo.hello"]},
        project,
        source_id="demo",
        owner_kind="extension",
        own_directories=(source,),
    )

    assert prompt in preserved
    assert "my own prompt" in prompt.read_text(encoding="utf-8")
    for path in owned:
        assert not path.exists(), path


def test_skill_cleanup_keeps_the_users_auxiliary_files(extension_dir, project_dir):
    """Only the owned ``SKILL.md`` goes; a file kept beside it survives."""
    _install_via_api(project_dir, extension_dir, "copilot", skills=True)
    skill_dir = project_dir / ".github" / "skills" / SKILL_NAME
    skill_file = skill_dir / "SKILL.md"
    assert skill_file.is_file()
    notes = skill_dir / "notes.txt"
    notes.write_text("my own notes\n", encoding="utf-8")

    result = _disable(project_dir)

    assert result.exit_code == 0, result.output
    assert not skill_file.exists()
    assert notes.is_file()
    assert notes.read_text(encoding="utf-8") == "my own notes\n"
    # The directory survives, so its provenance is recorded for the next enable.
    recorded = _registry_metadata(project_dir).get("preserved_skill_dirs", [])
    assert str(skill_dir.resolve()) in recorded

    # Enable must restore the entrypoint (the recorded provenance authorizes it)
    # without touching anything the user keeps there.
    runner = CliRunner()
    with patch.object(Path, "cwd", return_value=project_dir):
        result = runner.invoke(app, ["extension", "enable", "test-ext"], catch_exceptions=True)
    assert result.exit_code == 0, result.output
    assert skill_file.is_file()
    assert notes.read_text(encoding="utf-8") == "my own notes\n"

    # ... and a second disable/enable cycle behaves the same way.
    with patch.object(Path, "cwd", return_value=project_dir):
        assert runner.invoke(app, ["extension", "disable", "test-ext"], catch_exceptions=True).exit_code == 0
    assert not skill_file.exists()
    assert notes.read_text(encoding="utf-8") == "my own notes\n"
    with patch.object(Path, "cwd", return_value=project_dir):
        assert runner.invoke(app, ["extension", "enable", "test-ext"], catch_exceptions=True).exit_code == 0
    assert skill_file.is_file()
    assert notes.read_text(encoding="utf-8") == "my own notes\n"


def test_registration_keeps_user_owned_alias_and_prompt(tmp_path):
    """A refresh must not overwrite an alias or companion prompt the user took over.

    Registration may refresh its own outputs; the alias and prompt destinations get
    the same guard as the primary output, and skips are reported.
    """
    from specify_cli.agents import CommandRegistrar

    source = _bob_source(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _init_options(project, "copilot")
    registrar = CommandRegistrar(project)
    commands = [
        {
            "name": "speckit.demo.hello",
            "file": "commands/hello.md",
            "description": "d",
            "aliases": ["speckit.demo.alias"],
        }
    ]
    context_note = "\n<!-- Extension: demo -->\n<!-- Config: .specify/extensions/demo/ -->\n"
    registrar.register_commands(
        "copilot", commands, "demo", source, project,
        context_note=context_note, extension_id="demo",
    )
    alias_files = sorted(project.rglob("speckit.demo.alias*"))
    prompt_files = sorted(project.rglob("speckit.demo.alias.prompt.md"))
    assert alias_files, "the install wrote no alias output"
    assert prompt_files, "the install wrote no alias prompt"
    alias_file, prompt_file = alias_files[0], prompt_files[0]
    alias_file.write_text("MY OWN ALIAS\n", encoding="utf-8")
    prompt_file.write_text("---\nagent: mine\n---\nMY OWN PROMPT\n", encoding="utf-8")

    registrar.register_commands(
        "copilot", commands, "demo", source, project,
        context_note=context_note, extension_id="demo",
    )

    assert alias_file.read_text(encoding="utf-8") == "MY OWN ALIAS\n"
    assert "MY OWN PROMPT" in prompt_file.read_text(encoding="utf-8")
    preserved = {str(path) for path in registrar.preserved_paths}
    assert str(alias_file) in preserved
    assert str(prompt_file) in preserved

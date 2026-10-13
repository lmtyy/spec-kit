"""Legacy init-options absence must retain historical selector reconciliation."""

import pytest
from typer.testing import CliRunner

from specify_cli import app, save_init_options
from specify_cli.extensions import ExtensionManager
from specify_cli.extensions._commands import _refresh_presets_and_warn
from specify_cli.presets import PresetManager, PresetResolver
from tests.specify_cli.presets.test_install_transaction import tree_state
from tests.specify_cli.presets.test_selector_provider_lifecycle import (
    COMMAND,
    extension,
    preset,
    project,
    second_provider_extension,
)


@pytest.mark.parametrize("mutation", ["disable", "remove", "set-priority"])
def test_legacy_two_providers_reconcile_historical_outputs(
    tmp_path, monkeypatch, mutation
):
    root = project(tmp_path, monkeypatch)
    extensions = ExtensionManager(root)
    extensions.install_from_directory(extension(tmp_path), "0.1.5", priority=10)
    extensions.install_from_directory(
        second_provider_extension(tmp_path), "0.1.5", priority=20
    )
    manager = PresetManager(root)
    manager.install_from_directory(
        preset(
            tmp_path,
            "selector",
            r"regex:speckit\.provider\..*",
            "SELECTOR BODY",
            strategy="append",
        ),
        "0.1.5",
        priority=5,
    )
    save_init_options(root, {"ai": "copilot", "ai_skills": True, "script": "sh"})
    manager.register_enabled_presets_for_agent("copilot")
    command = root / ".gemini/commands" / f"{COMMAND}.toml"
    skill = root / ".github/skills/speckit-provider-collect/SKILL.md"
    for output in (command, skill):
        assert "EXTENSION BODY" in output.read_text()
    original_bytes = {output: output.read_bytes() for output in (command, skill)}
    # Preserve genuine recorded skill provenance in addition to detected roots.
    (root / ".specify/init-options.json").unlink()
    foreign = root / ".other-agent/skills/speckit-provider-collect/SKILL.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("---\nmetadata:\n  source: user:other\n---\nFOREIGN BYTES\n")
    foreign_bytes = foreign.read_bytes()
    args = ["extension", mutation, "provider"]
    args += (
        ["30"]
        if mutation == "set-priority"
        else ["--force"]
        if mutation == "remove"
        else []
    )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output + str(result.exception)
    for output in (command, skill):
        assert output.read_bytes() == original_bytes[output].replace(
            b"EXTENSION BODY", b"SECOND BODY"
        )
        assert "SECOND BODY" in output.read_text()
        assert "SELECTOR BODY" in output.read_text()
        assert "EXTENSION BODY" not in output.read_text()
    assert "preset:selector" in skill.read_text()
    metadata = PresetManager(root).registry.get("selector")
    assert COMMAND in metadata["registered_commands"]["gemini"]
    assert metadata["registered_skills"]["copilot"] == ["speckit-provider-collect"]
    provider = ExtensionManager(root).registry.get("provider")
    if mutation == "remove":
        assert provider is None
    else:
        assert provider["enabled"] is (mutation == "set-priority")
        assert provider["priority"] == (30 if mutation == "set-priority" else 10)
    layers = PresetResolver(root).collect_all_layers(COMMAND, "command")
    assert layers[1]["source"].startswith("extension:secondary ")
    before = tree_state(root)
    _refresh_presets_and_warn(root, {COMMAND}, strict=True)
    _refresh_presets_and_warn(root, {COMMAND}, strict=True)
    assert tree_state(root) == before
    if mutation == "set-priority":
        result = CliRunner().invoke(app, ["extension", "remove", "provider", "--force"])
        assert result.exit_code == 0, result.output
    result = CliRunner().invoke(app, ["extension", "remove", "secondary", "--force"])
    assert result.exit_code == 0, result.output
    assert not command.exists()
    assert not skill.parent.exists()
    assert foreign.read_bytes() == foreign_bytes
    metadata = PresetManager(root).registry.get("selector")
    assert metadata["enabled"] is True
    assert metadata["registered_commands"] == {}
    assert metadata["registered_skills"] == {}
    assert PresetResolver(root).collect_all_layers(COMMAND, "command") == []
    before = tree_state(root)
    _refresh_presets_and_warn(root, {COMMAND}, strict=True)
    assert tree_state(root) == before


@pytest.mark.parametrize("strict", [False, True])
def test_legacy_refresh_preserves_failure_contract(
    tmp_path, monkeypatch, strict, capsys
):
    root = project(tmp_path, monkeypatch)
    (root / ".specify/init-options.json").unlink()
    calls = []

    def fail(self, agent, **kwargs):
        calls.append((agent, kwargs))
        raise OSError("legacy registration failed")

    monkeypatch.setattr(PresetManager, "register_enabled_presets_for_agent", fail)
    if strict:
        with pytest.raises(OSError, match="legacy registration failed"):
            _refresh_presets_and_warn(root, {COMMAND}, strict=True)
        assert calls[0][1]["strict"] is True
    else:
        _refresh_presets_and_warn(root, {COMMAND})
        assert "legacy registration failed" in " ".join(capsys.readouterr().out.split())
        assert all("strict" not in kwargs for _, kwargs in calls)
    assert calls
    assert {agent for agent, _ in calls} <= {"gemini", "copilot"}


@pytest.mark.parametrize("contents", ["{}", "{bad", '{"ai": null}'])
def test_present_invalid_options_still_fail_closed(tmp_path, monkeypatch, contents):
    root = project(tmp_path, monkeypatch)
    (root / ".specify/init-options.json").write_text(contents)
    calls = []
    monkeypatch.setattr(
        PresetManager,
        "register_enabled_presets_for_agent",
        lambda *args, **kwargs: calls.append(args),
    )
    _refresh_presets_and_warn(root, {COMMAND}, strict=True)
    assert calls == []


@pytest.mark.parametrize("strict", [False, True])
def test_legacy_discovery_preserves_failure_contract(
    tmp_path, monkeypatch, strict, capsys
):
    root = project(tmp_path, monkeypatch)
    (root / ".specify/init-options.json").unlink()

    def fail(self, resolver):
        raise OSError("legacy discovery failed")

    monkeypatch.setattr(PresetManager, "_collect_selector_command_names", fail)
    if strict:
        with pytest.raises(OSError, match="legacy discovery failed"):
            _refresh_presets_and_warn(root, strict=True)
    else:
        _refresh_presets_and_warn(root)
        assert "legacy discovery failed" in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("mutation", ["disable", "remove", "set-priority"])
def test_legacy_refresh_never_overwrites_a_foreign_owned_skill(
    tmp_path, monkeypatch, mutation
):
    """Reconciliation must not take over a skill another owner declared.

    The historical pass is handed every recorded skills directory, and a legacy
    project revisits those same roots once init-options.json is gone, so a skills
    root can already hold an artifact owned by somebody else. Overwriting it
    replaces another owner's file with preset content while the CLI still exits 0.

    The target here is the REAL recognized skills root the fixture materializes
    (``.github/skills/...``), not a sibling directory outside the agent roots, so
    the assertion exercises ownership protection on a destination reconciliation
    can actually reach.
    """
    root = project(tmp_path, monkeypatch)
    extensions = ExtensionManager(root)
    extensions.install_from_directory(extension(tmp_path), "0.1.5", priority=10)
    extensions.install_from_directory(
        second_provider_extension(tmp_path), "0.1.5", priority=20
    )
    manager = PresetManager(root)
    manager.install_from_directory(
        preset(
            tmp_path,
            "selector",
            r"regex:speckit\.provider\..*",
            "SELECTOR BODY",
            strategy="append",
        ),
        "0.1.5",
        priority=5,
    )
    save_init_options(root, {"ai": "copilot", "ai_skills": True, "script": "sh"})
    manager.register_enabled_presets_for_agent("copilot")

    skill = root / ".github/skills/speckit-provider-collect/SKILL.md"
    assert skill.is_file(), "fixture must materialize the skill before it is replaced"

    # Somebody else's artifact now occupies the root the preset writes into.
    skill.write_text(
        "---\nmetadata:\n  source: user:other\n---\nFOREIGN BYTES\n",
        encoding="utf-8",
    )
    foreign_bytes = skill.read_bytes()

    (root / ".specify/init-options.json").unlink()

    args = ["extension", mutation, "provider"]
    args += (
        ["30"]
        if mutation == "set-priority"
        else ["--force"]
        if mutation == "remove"
        else []
    )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output + str(result.exception)
    assert skill.read_bytes() == foreign_bytes, (
        "reconciliation overwrote a SKILL.md owned by another source"
    )
    assert "FOREIGN BYTES" in skill.read_text(encoding="utf-8")


def test_legacy_refresh_still_reclaims_its_own_skill_and_supersedes_core(
    tmp_path, monkeypatch
):
    """The ownership guard must not stop a preset from managing its own output.

    A preset re-registering over its own ``preset:<id>`` skill has to overwrite
    it, and a marker-less file (a bundled core skill) must stay overridable, or
    the guard would trade a data-loss bug for a lost feature.
    """
    root = project(tmp_path, monkeypatch)
    extensions = ExtensionManager(root)
    extensions.install_from_directory(extension(tmp_path), "0.1.5", priority=10)
    extensions.install_from_directory(
        second_provider_extension(tmp_path), "0.1.5", priority=20
    )
    manager = PresetManager(root)
    manager.install_from_directory(
        preset(
            tmp_path,
            "selector",
            r"regex:speckit\.provider\..*",
            "SELECTOR BODY",
            strategy="append",
        ),
        "0.1.5",
        priority=5,
    )
    save_init_options(root, {"ai": "copilot", "ai_skills": True, "script": "sh"})
    manager.register_enabled_presets_for_agent("copilot")

    skill = root / ".github/skills/speckit-provider-collect/SKILL.md"
    assert "preset:selector" in skill.read_text(encoding="utf-8")

    # Own output: re-registration still overwrites it.
    _refresh_presets_and_warn(root, {COMMAND}, strict=True)
    assert "preset:selector" in skill.read_text(encoding="utf-8")
    assert "SELECTOR BODY" in skill.read_text(encoding="utf-8")

    # Marker-less file (the shape a bundled core skill has): still overridable.
    skill.write_text("---\ndescription: core\n---\nCORE BODY\n", encoding="utf-8")
    _refresh_presets_and_warn(root, {COMMAND}, strict=True)
    assert "SELECTOR BODY" in skill.read_text(encoding="utf-8")

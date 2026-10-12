"""M3: disabling a preset must retire skill-only output, however it was recorded.

``registered_skills`` is the only ownership record for a preset that wrote skills,
and it is also the only record that survives once the manifest can no longer be
read -- a safety-rejected selector makes ``_get_manifest`` return ``None``, which
empties the manifest declarations *and* the selector-matched command names. Before
the fix the disable path collected names from the manifest, from the selector
expansion and from ``registered_commands`` only, so the recorded skill names were
never turned back into command names and the installed SKILL.md stayed active
while the registry reported the preset as disabled.
"""

from __future__ import annotations

import warnings

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.extensions import ExtensionManager
from specify_cli.presets import PresetManager, PresetResolver
from tests.specify_cli.presets.test_install_transaction import tree_state
from tests.specify_cli.presets.test_selector_provider_lifecycle import (
    COMMAND,
    extension,
    preset,
    project,
    second_provider_extension,
)

SKILL = ".github/skills/speckit-provider-collect/SKILL.md"


def _skills_selector_project(tmp_path, monkeypatch, agent="copilot", skills=True):
    """Install two providers plus a selector preset, and materialize its skill."""
    root = project(tmp_path, monkeypatch, agent, skills)
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
    manager.register_enabled_presets_for_agent(agent)
    return root, manager


def _reject_manifest(root, preset_id="selector"):
    """Make a preset's manifest unreadable, exactly as a rejected selector does."""
    manifest = root / ".specify" / "presets" / preset_id / "preset.yml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace(r"regex:speckit\.provider\..*", r"regex:(a+)+$"),
                        encoding="utf-8")


def test_disable_retires_skill_when_the_manifest_is_rejected(tmp_path, monkeypatch):
    """TEST 3: a preset rejected by the safety boundary still gets cleaned up."""
    root, manager = _skills_selector_project(tmp_path, monkeypatch)
    skill = root / SKILL
    assert skill.is_file(), "fixture must materialize the skill first"
    assert "preset:selector" in skill.read_text(encoding="utf-8")

    # The manifest is now unreadable (the selector would be rejected), so the
    # declarations and the selector-matched names are both empty.
    _reject_manifest(root)
    metadata = manager.registry.get("selector")
    assert metadata["registered_skills"], "the skill ownership must survive"

    result = CliRunner().invoke(app, ["preset", "disable", "selector"])
    assert result.exit_code == 0, result.output + str(result.exception)

    # A surviving provider (the second extension) legitimately keeps the artifact,
    # so the invariant is that the *disabled preset's* contribution is gone.
    assert skill.read_text(encoding="utf-8") != "", "skill vanished entirely"
    text = skill.read_text(encoding="utf-8")
    assert "preset:selector" not in text, "the disabled preset still owns the skill"
    assert "SELECTOR BODY" not in text, "the rejected preset's skill body was left active"
    metadata = PresetManager(root).registry.get("selector")
    assert metadata["enabled"] is False
    assert not metadata["registered_skills"]
    assert not metadata["registered_commands"]


def test_disable_is_idempotent_for_skill_only_output(tmp_path, monkeypatch):
    """TEST 10: repeating the disable is a no-op, not a second mutation."""
    root, manager = _skills_selector_project(tmp_path, monkeypatch)
    skill = root / SKILL
    _reject_manifest(root)

    first = CliRunner().invoke(app, ["preset", "disable", "selector"])
    assert first.exit_code == 0, first.output
    assert "SELECTOR BODY" not in skill.read_text(encoding="utf-8")
    assert PresetManager(root).registry.get("selector")["enabled"] is False

    state = tree_state(root)
    second = CliRunner().invoke(app, ["preset", "disable", "selector"])
    assert second.exit_code == 0, second.output
    assert tree_state(root) == state, "a repeated disable mutated the tree"
    assert "SELECTOR BODY" not in skill.read_text(encoding="utf-8")


def test_disable_rebuilds_the_skill_from_a_surviving_provider(tmp_path, monkeypatch):
    """TEST 4: with another provider left, the skill is restored, not deleted."""
    root, manager = _skills_selector_project(tmp_path, monkeypatch)
    skill = root / SKILL
    assert "preset:selector" in skill.read_text(encoding="utf-8")

    result = CliRunner().invoke(app, ["preset", "disable", "selector"])
    assert result.exit_code == 0, result.output + str(result.exception)

    layers = PresetResolver(root).collect_all_layers(COMMAND, "command")
    if layers:
        assert skill.is_file(), "a surviving provider's skill must still be present"
        assert "SELECTOR BODY" not in skill.read_text(encoding="utf-8")
    metadata = PresetManager(root).registry.get("selector")
    assert metadata["enabled"] is False


def test_skill_name_recovery_never_invents_an_identity(tmp_path):
    """TEST 6: similar-looking names in different namespaces never get merged.

    ``speckit.a-b`` and ``speckit.a.b`` both render their skill directory as
    ``speckit-a-b``, so the modern skill name alone does not identify a command.
    Every candidate offered must reproduce the recorded name through the real
    registration rule, and the exact legacy form must survive as itself.
    """
    manager = PresetManager(tmp_path)
    resolver = PresetResolver(tmp_path)
    recover = manager._command_names_for_skill_names

    # Legacy form inverts exactly.
    assert recover(["speckit.a.b"], resolver) == {"speckit.a.b"}

    # Modern form is ambiguous: both round-tripping candidates are offered,
    # and nothing that fails the round trip is.
    candidates = recover(["speckit-a-b"], resolver)
    assert candidates == {"speckit.a.b", "speckit.a-b"}
    for candidate in candidates:
        modern, legacy = manager._skill_names_for_command(candidate)
        assert "speckit-a-b" in (modern, legacy)

    # A name that cannot be a command name at all yields nothing.
    assert recover(["not-a-speckit-name"], resolver) == set()
    assert recover(["speckit-"], resolver) == set()

    # Names past the documented component bound are left for retry, not enumerated.
    deep = "speckit-" + "-".join(f"c{i}" for i in range(manager._MAX_SKILL_NAME_COMPONENTS + 1))
    with pytest.warns(UserWarning, match="component"):
        assert recover([deep], resolver) == set()


def test_partial_skill_cleanup_failure_keeps_provenance_for_retry(
    tmp_path, monkeypatch
):
    """TEST 7/8: a cleanup failure must not report success or drop ownership."""
    root, manager = _skills_selector_project(tmp_path, monkeypatch)
    skill = root / SKILL
    _reject_manifest(root)
    before = manager.registry.get("selector")["registered_skills"]
    assert before

    def explode(*args, **kwargs):
        raise OSError("simulated skill cleanup failure")

    monkeypatch.setattr(PresetManager, "_reconcile_skills", explode)
    result = CliRunner().invoke(app, ["preset", "disable", "selector"])
    assert result.exit_code == 0, result.output
    assert "cleanup failed" in result.output.lower() or "remain recorded" in result.output

    metadata = PresetManager(root).registry.get("selector")
    assert metadata["enabled"] is False
    assert metadata["registered_skills"], "ownership was dropped before cleanup succeeded"
    assert skill.is_file(), "the failed cleanup must leave the file for a retry"


def test_command_only_output_still_reconciles(tmp_path, monkeypatch):
    """TEST 1/2 complement: command-mode output must not regress."""
    root = project(tmp_path, monkeypatch, "gemini", False)
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
    extensions = ExtensionManager(root)
    extensions.install_from_directory(extension(tmp_path), "0.1.5", priority=10)
    manager.register_enabled_presets_for_agent("gemini")

    command = root / ".gemini" / "commands" / f"{COMMAND}.toml"
    assert command.is_file()
    assert "SELECTOR BODY" in command.read_text(encoding="utf-8")
    result = CliRunner().invoke(app, ["preset", "disable", "selector"])
    assert result.exit_code == 0, result.output + str(result.exception)
    # The extension provider survives, so the command file stays -- but the
    # disabled preset's contribution must be gone.
    assert "SELECTOR BODY" not in command.read_text(encoding="utf-8")
    metadata = PresetManager(root).registry.get("selector")
    assert metadata["enabled"] is False
    assert not metadata["registered_commands"]


def test_skill_names_past_the_component_bound_are_reported_not_dropped(tmp_path):
    """AUD-1: a still-installed skill must never be dropped in silence.

    Reversing a modern skill name is ambiguous -- every hyphen may have been a dot --
    so candidates are enumerated within a bound. Past that bound the identity cannot
    be resolved, but the skill is still on disk: dropping it quietly let ``preset
    disable`` report success while leaving the artifact active.
    """
    manager = PresetManager(tmp_path)
    resolver = PresetResolver(tmp_path)
    recover = manager._command_names_for_skill_names
    bound = manager._MAX_SKILL_NAME_COMPONENTS

    # Up to the bound every recorded name is still resolvable. This used to stop at 8.
    for count in (9, bound):
        name = "speckit-" + "-".join(f"c{i}" for i in range(count))
        candidates = recover([name], resolver)
        assert candidates, f"{count} components produced no candidate"
        assert all(name in manager._skill_names_for_command(c) for c in candidates)

    # Past the bound it is reported, and the identity is not guessed at.
    over = "speckit-" + "-".join(f"c{i}" for i in range(bound + 1))
    with pytest.warns(UserWarning, match="bound for resolving its command ownership"):
        assert recover([over], resolver) == set()


def test_degenerate_legacy_names_are_not_accepted_as_commands(tmp_path):
    """AUD-2: only shapes the registration rule can produce may be inverted."""
    manager = PresetManager(tmp_path)
    resolver = PresetResolver(tmp_path)
    recover = manager._command_names_for_skill_names

    # No rule ever generated these, so they are not concrete command names.
    for bad in ["speckit.", "speckit.a.", "speckit..b", "speckit.a..b", "speckit.."]:
        assert recover([bad], resolver) == set(), bad

    # The legitimate legacy forms are untouched, hyphenated short name included.
    for good in ["speckit.a", "speckit.a.b", "speckit.a-b", "speckit.a.b-c"]:
        assert recover([good], resolver) == {good}, good


def test_a_disabled_preset_with_a_deep_skill_name_reports_the_unresolved_skill(
    tmp_path, monkeypatch
):
    """AUD-1 end to end: the operator sees the unresolved skill instead of success."""
    root, manager = _skills_selector_project(tmp_path, monkeypatch)
    over = "speckit-" + "-".join(f"c{i}" for i in range(manager._MAX_SKILL_NAME_COMPONENTS + 1))
    manager.registry.update("selector", {"registered_skills": {"copilot": [over]}})

    # Catch the warning directly rather than asserting on captured CLI output, so
    # the contract under test is "the operator is told", not how the runner buffers.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = CliRunner().invoke(app, ["preset", "disable", "selector"])
    assert result.exit_code == 0, result.output
    messages = [str(item.message) for item in caught]
    assert any("may remain active" in message for message in messages), messages

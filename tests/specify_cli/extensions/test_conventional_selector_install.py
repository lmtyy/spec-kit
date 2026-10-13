"""Convention-only providers materialize through the real install transaction."""

import pytest
import yaml

from specify_cli.extensions import ExtensionManager, HookExecutor
from specify_cli.presets import PresetManager, PresetResolver
from tests.specify_cli.presets.test_install_transaction import tree_state
from tests.specify_cli.presets.test_selector_provider_lifecycle import (
    COMMAND,
    extension,
    preset,
    project,
)


def conventional_source(tmp_path, identifier, relative, body, declared=False):
    staging = tmp_path / identifier
    staging.mkdir()
    source = extension(staging)
    path = source / "extension.yml"
    manifest = yaml.safe_load(path.read_text())
    manifest["extension"]["id"] = identifier
    manifest["provides"]["commands"] = (
        [{"name": COMMAND, "file": relative, "description": "Declared overlap"}]
        if declared
        else []
    )
    # Empty commands is valid when the extension also supplies a non-command
    # resource; do not bypass manifest validation to reach the installer.
    manifest["provides"]["templates"] = [
        {"name": "example-template", "file": "example.md"}
    ]
    (source / "example.md").write_text("Example template\n")
    path.write_text(yaml.safe_dump(manifest))
    (source / "commands/body.md").unlink()
    payload = source / relative
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text(f"---\ndescription: Provider\n---\n{body}\n")
    return source


def output_path(root, skills):
    return root / (
        ".github/skills/speckit-provider-collect/SKILL.md"
        if skills
        else f".gemini/commands/{COMMAND}.toml"
    )


@pytest.mark.parametrize(
    "relative",
    [
        f"commands/{COMMAND}.md",
        "commands/provider.collect.md",
        f"templates/commands/{COMMAND}.md",
        "templates/commands/provider.collect.md",
    ],
)
@pytest.mark.parametrize("skills", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_empty_manifest_conventional_install_is_immediate_and_atomic(
    tmp_path, monkeypatch, relative, skills, fail
):
    root = project(tmp_path, monkeypatch, "copilot" if skills else "gemini", skills)
    PresetManager(root).install_from_directory(
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
    manager = ExtensionManager(root)
    source = conventional_source(tmp_path, "provider", relative, "CONVENTIONAL BODY")
    target = output_path(root, skills)
    assert not target.exists()
    before = tree_state(root)
    before_registry = manager.registry.data.copy()
    preset_registry = (root / ".specify/presets/.registry").read_bytes()
    calls = []
    original = PresetManager._register_commands

    def record(self, manifest, pack_dir, **kwargs):
        calls.extend(item["name"] for item in kwargs.get("command_templates", []))
        return original(self, manifest, pack_dir, **kwargs)

    monkeypatch.setattr(PresetManager, "_register_commands", record)

    def fail_hooks(self, manifest):
        assert "CONVENTIONAL BODY" in target.read_text()
        assert "SELECTOR BODY" in target.read_text()
        raise OSError("failure after conventional materialization")

    if fail:
        monkeypatch.setattr(HookExecutor, "register_hooks", fail_hooks)
        with pytest.raises(OSError, match="after conventional materialization"):
            manager.install_from_directory(source, "0.1.5")
        assert not target.exists()
        if skills:
            assert not target.parent.exists()
        assert ExtensionManager(root).registry.data == before_registry
        assert (root / ".specify/presets/.registry").read_bytes() == preset_registry
        operational = {
            ".specify/extensions",
            ".specify/extensions/.backup",
            ".specify/extensions/.backup/provider",
        }
        assert {k: v for k, v in tree_state(root).items() if k not in operational} == {
            k: v for k, v in before.items() if k not in operational
        }
    else:
        manifest = manager.install_from_directory(source, "0.1.5")
        assert manifest.commands == []
        assert "CONVENTIONAL BODY" in target.read_text()
        assert "SELECTOR BODY" in target.read_text()
        layers = PresetResolver(root).collect_all_layers(COMMAND, "command")
        assert layers[1]["path"] == root / ".specify/extensions/provider" / relative
        metadata = PresetManager(root).registry.get("selector")
        provider = ExtensionManager(root).registry.get("provider")
        assert provider["enabled"] is True
        if skills:
            assert metadata["registered_skills"]["copilot"] == [
                "speckit-provider-collect"
            ]
            assert provider["registered_skills"] == ["speckit-provider-collect"]
            assert "preset:selector" in target.read_text()
        else:
            assert metadata["registered_commands"]["gemini"] == [COMMAND]
            assert provider["registered_commands"]["gemini"] == [COMMAND]
    assert calls == [COMMAND]


@pytest.mark.parametrize("skills", [False, True])
def test_conventional_install_resolves_priority_and_declared_overlap_once(
    tmp_path, monkeypatch, skills
):
    root = project(tmp_path, monkeypatch, "copilot" if skills else "gemini", skills)
    PresetManager(root).install_from_directory(
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
    extensions.install_from_directory(
        conventional_source(
            tmp_path, "secondary", f"commands/{COMMAND}.md", "HIGH PRIORITY BODY"
        ),
        "0.1.5",
        priority=10,
    )
    calls = []
    original = PresetManager._register_commands

    def record(self, manifest, pack_dir, **kwargs):
        calls.extend(item["name"] for item in kwargs.get("command_templates", []))
        return original(self, manifest, pack_dir, **kwargs)

    monkeypatch.setattr(PresetManager, "_register_commands", record)
    extensions.install_from_directory(
        conventional_source(
            tmp_path,
            "provider",
            f"commands/{COMMAND}.md",
            "LOW PRIORITY BODY",
            declared=True,
        ),
        "0.1.5",
        priority=20,
    )
    target = output_path(root, skills)
    assert "HIGH PRIORITY BODY" in target.read_text()
    assert "LOW PRIORITY BODY" not in target.read_text()
    assert "SELECTOR BODY" in target.read_text()
    assert calls == [COMMAND]
    layers = PresetResolver(root).collect_all_layers(COMMAND, "command")
    assert layers[1]["source"].startswith("extension:secondary ")
    metadata = PresetManager(root).registry.get("selector")
    if skills:
        assert metadata["registered_skills"]["copilot"] == ["speckit-provider-collect"]
        assert "preset:selector" in target.read_text()
    else:
        assert metadata["registered_commands"]["gemini"] == [COMMAND]


@pytest.mark.parametrize("skills", [False, True])
def test_conventional_candidate_install_is_idempotent_on_reinstall(
    tmp_path, monkeypatch, skills
):
    """Reinstalling a conventional-only provider must not duplicate its output."""
    root = project(tmp_path, monkeypatch, "copilot" if skills else "gemini", skills)
    PresetManager(root).install_from_directory(
        preset(tmp_path, "selector", r"regex:speckit\.provider\..*", "SELECTOR BODY", strategy="append"),
        "0.1.5",
        priority=5,
    )
    extensions = ExtensionManager(root)
    source = conventional_source(tmp_path, "secondary", f"commands/{COMMAND}.md", "PROVIDER BODY")
    extensions.install_from_directory(source, "0.1.5", priority=10)
    target = output_path(root, skills)
    first = target.read_text()
    before_files = {p.relative_to(root) for p in root.rglob("*") if p.is_file()}
    def ownership(meta):
        return (
            meta.get("enabled"),
            meta.get("priority"),
            meta.get("registered_commands"),
            meta.get("registered_skills"),
        )

    before_ownership = ownership(ExtensionManager(root).registry.get("secondary"))

    extensions.install_from_directory(source, "0.1.5", priority=10, force=True)

    assert target.read_text() == first
    assert "PROVIDER BODY" in target.read_text()
    # A reinstall rewrites the same destination: no file appears or disappears...
    assert {p.relative_to(root) for p in root.rglob("*") if p.is_file()} == before_files
    # ...and the recorded ownership is unchanged (no duplicate registration).
    assert ownership(ExtensionManager(root).registry.get("secondary")) == before_ownership


@pytest.mark.parametrize("skills", [False, True])
def test_conventional_candidate_follows_the_new_winner(tmp_path, monkeypatch, skills):
    """Adding a higher-precedence provider republishes the winner's content once."""
    root = project(tmp_path, monkeypatch, "copilot" if skills else "gemini", skills)
    PresetManager(root).install_from_directory(
        preset(tmp_path, "selector", r"regex:speckit\.provider\..*", "SELECTOR BODY", strategy="append"),
        "0.1.5",
        priority=5,
    )
    extensions = ExtensionManager(root)
    extensions.install_from_directory(
        conventional_source(tmp_path, "first", f"commands/{COMMAND}.md", "FIRST BODY"),
        "0.1.5",
        priority=20,
    )
    target = output_path(root, skills)
    assert "FIRST BODY" in target.read_text()

    extensions.install_from_directory(
        conventional_source(tmp_path, "second", f"commands/{COMMAND}.md", "SECOND BODY"),
        "0.1.5",
        priority=10,
    )

    content = target.read_text()
    assert "SECOND BODY" in content
    assert "FIRST BODY" not in content
    assert len(list(target.parents[1].rglob("SKILL.md" if skills else "*.toml"))) == 1
    layers = PresetResolver(root).collect_all_layers(COMMAND, "command")
    assert layers[1]["source"].startswith("extension:second ")


@pytest.mark.parametrize("skills", [False, True])
def test_conventional_install_materializes_project_override(tmp_path, monkeypatch, skills):
    """A project override wins resolution and must reach the agent during install.

    A convention-only candidate carries no staged payload of its own, so the
    winning override has to be published by the install transaction itself:
    nothing may depend on a later lifecycle refresh to make the install correct.
    """
    root = project(tmp_path, monkeypatch, "copilot" if skills else "gemini", skills)
    overrides = root / ".specify/templates/overrides"
    overrides.mkdir(parents=True, exist_ok=True)
    (overrides / f"{COMMAND}.md").write_text(
        "PROJECT OVERRIDE BODY\n", encoding="utf-8"
    )
    manager = ExtensionManager(root)
    source = conventional_source(
        tmp_path, "provider", f"commands/{COMMAND}.md", "CONVENTIONAL BODY"
    )
    target = output_path(root, skills)
    assert not target.exists()

    manager.install_from_directory(source, "1.0.0", force=True, register_commands=True)

    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert "PROJECT OVERRIDE BODY" in body
    assert "CONVENTIONAL BODY" not in body

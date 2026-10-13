"""Exact pre-mutation artifact snapshots shared by preset transactions."""

from pathlib import Path
import shutil
import tempfile


def refresh_journal_identities(project_root, roots=()):
    """Record the current identity of the artifacts this operation wrote.

    The integration journal compares the identity it stored for a path with the
    file at rollback time; a record left behind as "did not exist" (what a writer
    that is not itself journaled produces) reads as a concurrent edit, and the
    transaction then keeps its recovery snapshots instead of completing its
    rollback. Re-recording the final state of the artifacts named in ``roots``
    keeps that view accurate.

    Only those scopes are refreshed: every other journal entry keeps the identity
    recorded when it was written, so a genuine concurrent edit elsewhere is still
    reported as a conflict. Callers pass the paths the operation actually
    materialized; an empty ``roots`` refreshes nothing.
    """
    from ..integrations._file_changes import after_file_change, file_change_observer

    if not roots:
        return
    observer = file_change_observer.get()
    journal = getattr(observer, "__self__", None) if observer is not None else None
    if journal is None:
        return
    changes = getattr(journal, "changes", None)
    if not hasattr(changes, "items"):
        return
    root = Path(project_root)
    scopes = tuple(Path(item) for item in roots)
    for path in list(changes):
        try:
            if not path.is_relative_to(root) or not path.exists():
                continue
            if not any(path == scope or path.is_relative_to(scope) for scope in scopes):
                continue
            after_file_change(path)
        except Exception:  # noqa: BLE001 - bookkeeping refresh
            # This runs from callers' ``finally`` blocks: a refresh that cannot be
            # performed must never replace the install's own error, and the
            # journal's conflict detection remains the safety net either way.
            continue


class _ArtifactSnapshot:
    """Back up exact artifact trees, including absent paths and symlinks."""

    def __init__(self):
        # ``ignore_cleanup_errors`` matters on Windows: a locked backup file
        # would otherwise make ``close()`` raise from a ``finally`` and replace
        # the operation's own exception with a cleanup error.
        self._temp = tempfile.TemporaryDirectory(
            prefix="preset-transaction-", ignore_cleanup_errors=True
        )
        self._paths = {}
        self._absent_parents = set()
        # Directory-link type per captured symlink: Windows needs
        # ``target_is_directory`` to recreate a directory link (and to create
        # the backup link itself when the target is a directory).
        self._symlink_is_dir = {}
        self.retained_path = None
        # Paths the last ``restore()`` actually put back, for callers that must
        # tell a concurrent transaction those paths are already handled.
        self.restored_paths: tuple = ()

    def capture(self, path):
        path = Path(path)
        for parent in path.parents:
            if parent.exists():
                break
            self._absent_parents.add(parent)
        if path in self._paths:
            return
        backup = Path(self._temp.name) / str(len(self._paths))
        if path.is_symlink():
            # The link's own type, not the target's: a directory link whose target is
            # missing would otherwise be recorded (and later recreated) as a file link,
            # and Windows cannot change a link's type after creation.
            from .._utils import link_targets_directory

            is_dir = link_targets_directory(path)
            self._symlink_is_dir[path] = is_dir
            backup.symlink_to(path.readlink(), target_is_directory=is_dir)
        elif path.is_dir():
            shutil.copytree(path, backup, symlinks=True)
        elif path.exists():
            shutil.copy2(path, backup)
        self._paths[path] = backup

    def captured_paths(self):
        """The artifacts this snapshot captured, i.e. what its owner wrote."""
        return tuple(self._paths)

    def retain(self):
        """Keep the backups for manual recovery after a failed restore.

        Without this a failed restore would delete the live artifact *and* its
        only backup, leaving the user nothing to recover from. Cleanup is
        disarmed by moving the directory aside; the caller is told the path.
        """
        if self.retained_path is not None:
            return self.retained_path
        source = Path(self._temp.name)
        if not source.exists():
            return None
        retained = source.with_name(f"{source.name}-retained")
        try:
            source.rename(retained)
        except OSError:
            return None
        self.retained_path = retained
        return retained

    def restore(self):
        errors = []
        restored = []
        for path, backup in self._paths.items():
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.exists():
                    shutil.rmtree(path)
                if backup.is_symlink():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.symlink_to(
                        backup.readlink(),
                        target_is_directory=self._symlink_is_dir.get(
                            path, backup.is_dir()
                        ),
                    )
                elif backup.is_dir():
                    shutil.copytree(backup, path, symlinks=True)
                elif backup.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
            else:
                restored.append(path)
        for parent in sorted(
            self._absent_parents, key=lambda p: len(p.parts), reverse=True
        ):
            if parent.is_dir():
                try:
                    parent.rmdir()
                except OSError as exc:
                    errors.append(f"{parent}: {exc}")
                else:
                    restored.append(parent)
        self.restored_paths = tuple(restored)
        if errors:
            # Retain the backups *before* raising: the failed paths may no
            # longer exist, so their backup copies are the only recovery source.
            retained = self.retain()
            detail = f" (retained backups: {retained})" if retained else ""
            raise OSError(
                "Could not restore preset artifacts: " + "; ".join(errors) + detail
            )

    def close(self):
        # A retained snapshot has already been moved aside: cleanup must not
        # remove the recovery copies. Cleanup itself is best-effort (see
        # ``ignore_cleanup_errors``) so it never masks the active exception.
        self._temp.cleanup()


def _capture_preset_artifacts(manager, snapshot, extra_commands=()):
    """Use registrar/skill directory resolution, not a second selector parser."""
    from ..agents import CommandRegistrar
    from ..shared_infra import _validate_safe_shared_directory

    root = manager.project_root
    # Registries contain ownership updates for winners other than the reordered
    # preset too. Constitution bytes and provenance must move together.
    snapshot.capture(manager.registry.registry_path)
    snapshot.capture(root / ".specify" / "extensions" / ".registry")
    snapshot.capture(root / ".specify" / "memory")
    snapshot.capture(root / ".github" / "prompts")
    for pack_id in manager.registry.keys():  # noqa: SIM118 - registry is not a mapping
        snapshot.capture(manager.presets_dir / pack_id / ".composed")
    registrar = CommandRegistrar(root)
    for agent, config in registrar.AGENT_CONFIGS.items():
        if config.get("extension") == "/SKILL.md":
            continue
        directory = registrar._resolve_agent_dir(agent, config, root)
        if directory.is_relative_to(root):
            _validate_safe_shared_directory(root, directory)
            snapshot.capture(directory)
        legacy = config.get("legacy_dir")
        if legacy:
            directory = root / legacy
            _validate_safe_shared_directory(root, directory)
            snapshot.capture(directory)
    # Native and skills-mode output roots can be global.
    from .. import load_init_options

    opts = load_init_options(root)
    agents = {opts.get("ai")} if isinstance(opts, dict) else set()
    if not opts:
        # Legacy projects detect every existing integration during registration.
        agents.update(registrar.AGENT_CONFIGS)
    for metadata in manager.registry.list().values():
        commands = metadata.get("registered_commands")
        if isinstance(commands, dict):
            agents.update(commands)
        skills = metadata.get("registered_skills")
        if isinstance(skills, dict):
            agents.update(skills)
    for agent in agents:
        if not isinstance(agent, str) or agent == "generic":
            continue
        directory = manager._resolve_agent_skills_dir(agent)
        validation_root = manager._skills_validation_root(directory)
        if validation_root is None:
            continue
        _validate_safe_shared_directory(validation_root, directory)
        if directory.is_relative_to(root):
            snapshot.capture(directory)
        else:
            from ..artifacts import ArtifactCatalog
            from ..extensions import CORE_COMMAND_NAMES

            names = {f"speckit.{name}" for name in CORE_COMMAND_NAMES}
            names.update(extra_commands)
            names.update(
                artifact.name
                for artifact in ArtifactCatalog(root).list_artifacts()
                if artifact.kind == "command"
            )
            candidates = {
                skill
                for name in names
                for skill in manager._skill_names_for_command(name)
                if manager._is_safe_registry_skill_name(skill)
            }
            if directory.is_dir():
                candidates.update(
                    child.name
                    for child in directory.iterdir()
                    if child.name.startswith(("speckit-", "speckit."))
                )
            for metadata in manager.registry.list().values():
                recorded = metadata.get("registered_skills")
                lists = recorded.values() if isinstance(recorded, dict) else [recorded]
                for values in lists:
                    if isinstance(values, list):
                        candidates.update(
                            name
                            for name in values
                            if manager._is_safe_registry_skill_name(name)
                        )
            for name in candidates:
                snapshot.capture(directory / name)

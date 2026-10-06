"""The QwenPaw plugin mirrors the other agent plugin manifests.

`.qwenpaw-plugin/` sits beside `.claude-plugin/` and `.codex-plugin/` as an
installer-facing plugin package. Its manifest must carry the release version
(stamped from VERSION by `scripts/release/sync-version.mjs`), and its entry
point must import cleanly without QwenPaw installed — the QwenPaw loader
validates a plugin by importing the backend entry and requiring a `plugin`
instance, and a checkout has no `qwenpaw` package.

Unlike the repo-root plugin packages, the QwenPaw loader installs a plugin by
copying its one directory, so a skill tree cannot be referenced from outside
it. Nothing is duplicated in git for that purpose: the entry point resolves a
`skills/` tree at runtime, and these tests pin the resolution order and, more
importantly, that a configured path which fails to resolve is reported instead
of being quietly served by a stale copy.

The `/cad-setup` contract is pinned too, against the failure modes a review
found: its subprocesses must not run on QwenPaw's event loop (the handler is
awaited there), a doctor failure must surface stderr (where the mismatch and
its repair instructions go), and the once-only fabrication gate must not mark
a workspace provisioned until the gated entries read back disabled.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / ".qwenpaw-plugin"
MANIFEST_PATH = PLUGIN_DIR / "plugin.json"

VALID_QWENPAW_TYPES = {
    "tool",
    "provider",
    "hook",
    "command",
    "channel",
    "frontend",
    "app",
    "general",
}


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def load_entry(test: unittest.TestCase):
    """Import the plugin entry point, cleaned out of `sys.modules` afterwards.

    The resolver reads its own module-level `PLUGIN_DIR`, so tests that move it
    around must not leave the module cached with someone else's path in it.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_test_qwenpaw_plugin_entry", PLUGIN_DIR / "plugin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    test.addCleanup(sys.modules.pop, "_test_qwenpaw_plugin_entry", None)
    return module


class QwenPawPluginManifestTest(unittest.TestCase):
    def test_manifest_exists(self) -> None:
        self.assertTrue(
            MANIFEST_PATH.is_file(),
            "missing .qwenpaw-plugin/plugin.json",
        )

    def test_manifest_has_the_required_fields(self) -> None:
        manifest = load_manifest()
        for field in ("id", "version", "name"):
            self.assertTrue(
                str(manifest.get(field, "")).strip(),
                f"plugin.json must declare a non-empty {field!r}",
            )

    def test_manifest_type_is_a_known_plugin_type(self) -> None:
        self.assertIn(load_manifest().get("type"), VALID_QWENPAW_TYPES)

    def test_backend_entry_is_declared(self) -> None:
        entry = load_manifest().get("entry") or {}
        self.assertTrue(
            entry.get("backend"),
            "entry.backend must name the Python entry file",
        )
        self.assertTrue(
            (PLUGIN_DIR / entry["backend"]).is_file(),
            "entry.backend must exist in the plugin directory",
        )

    def test_version_matches_the_canonical_release_version(self) -> None:
        version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(
            load_manifest().get("version"),
            version,
            ".qwenpaw-plugin/plugin.json version must equal VERSION "
            "(scripts/release/sync-version.mjs stamps it)",
        )

    def test_entry_imports_without_qwenpaw_installed(self) -> None:
        """The loader imports the entry and requires a `plugin` instance.

        The module must keep its importable surface stdlib-only (the
        qwenpaw import sits under `if TYPE_CHECKING`), so validation
        succeeds in a checkout and on any machine before dependencies
        are installed.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_test_qwenpaw_plugin_entry", PLUGIN_DIR / "plugin.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            self.assertTrue(hasattr(module, "plugin"), "must export `plugin`")
            self.assertTrue(hasattr(module.plugin, "register"))
        finally:
            sys.modules.pop("_test_qwenpaw_plugin_entry", None)

    def test_skills_dir_resolves_to_the_canonical_tree(self) -> None:
        """With no config and no generated copy, the checkout sibling resolves.

        This is the layout the repository itself now ships: one `skills/` tree,
        no second copy in git. An install that generated the bundled copy, or
        a config naming a path, is covered by the tests below.
        """
        module = load_entry(self)
        resolved = module._resolve_skills_dir()
        self.assertEqual(
            resolved,
            REPO_ROOT / "skills",
            "the checkout sibling must resolve",
        )
        self.assertEqual(
            module._SKILLS_STATE["resolved"],
            "checkout sibling",
            "the reported source must name what won, for /cad-setup",
        )

    def test_configured_skills_dir_outranks_local_candidates(self) -> None:
        """plugins.cad.skills_dir wins, which is what avoids the duplicate."""
        module = load_entry(self)
        with tempfile.TemporaryDirectory() as tmp:
            configured = Path(tmp) / "checkout" / "skills"
            (configured / "cad").mkdir(parents=True)
            (configured / "cad" / "SKILL.md").write_text("# cad\n", encoding="utf-8")
            self.assertEqual(
                module._resolve_skills_dir({"skills_dir": str(configured)}),
                configured,
            )
            self.assertEqual(module._SKILLS_STATE["resolved"], "configured")

    def test_bundled_copy_is_preferred_over_the_checkout_sibling(self) -> None:
        """An install that generated the copy uses it, not the repo tree.

        The copy is the documented answer for a user with no checkout to point
        at, so it has to win over whatever sibling happens to exist.
        """
        module = load_entry(self)
        original = module.PLUGIN_DIR
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "plugins" / "cad"
            for tree in (plugin_dir / "skills", plugin_dir.parent / "skills"):
                (tree / "cad").mkdir(parents=True)
                (tree / "cad" / "SKILL.md").write_text("# cad\n", encoding="utf-8")
            module.PLUGIN_DIR = plugin_dir
            try:
                self.assertEqual(
                    module._resolve_skills_dir(),
                    plugin_dir / "skills",
                )
                self.assertEqual(
                    module._SKILLS_STATE["resolved"],
                    "bundled copy",
                )
            finally:
                module.PLUGIN_DIR = original

    def test_stale_configured_path_is_reported_and_not_fallen_back_through(
        self,
    ) -> None:
        """A bad pointer is a misconfiguration, not a licence to serve a copy.

        Falling through to the bundled copy here would let a moved checkout keep
        provisioning skills nobody updated, with the error buried in a log line
        nobody reads. Resolution stops, and the state says so.
        """
        module = load_entry(self)
        original = module.PLUGIN_DIR
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "plugins" / "cad"
            (plugin_dir / "skills" / "cad").mkdir(parents=True)
            (plugin_dir / "skills" / "cad" / "SKILL.md").write_text(
                "# cad\n", encoding="utf-8"
            )
            module.PLUGIN_DIR = plugin_dir
            try:
                with self.assertLogs(module.logger, level="ERROR") as logs:
                    self.assertIsNone(
                        module._resolve_skills_dir(
                            {"skills_dir": str(Path(tmp) / "moved-away")}
                        )
                    )
                self.assertIn("not a skills tree", " ".join(logs.output))
                self.assertEqual(
                    module._SKILLS_STATE["resolved"],
                    "configured path invalid",
                )
                # The reported path is the whole thing: /x/moved-away/skills
                # shown as "skills" tells the user nothing about where to look.
                self.assertEqual(
                    module._SKILLS_STATE["path"],
                    str(Path(tmp) / "moved-away"),
                )
            finally:
                module.PLUGIN_DIR = original

    def test_skills_dir_missing_layout_does_not_crash_register(self) -> None:
        """A plugin copy with no skills/ anywhere near it must not raise.

        The loader's install hook silently no-ops on a missing skills_dir;
        if the resolver returned a nonexistent path, every startup would
        provision nothing and only a log line would say why. register()
        must take the skip branch and still register the rest.
        """
        import importlib.util
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            orphan = Path(tmp) / "plugin.py"
            orphan.write_text(
                (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            spec = importlib.util.spec_from_file_location(
                "_test_qwenpaw_plugin_orphan", orphan
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            try:
                self.assertIsNone(module._resolve_skills_dir())
                registered: list[str] = []

                class FakeApi:
                    def register_skill_provider(self, skills_dir, **kwargs):
                        registered.append("skills")

                    def register_slash_command(self, name, handler, **kwargs):
                        registered.append(f"slash:{name}")

                    def register_startup_hook(self, hook_name, callback, priority=100):
                        registered.append(f"hook:{hook_name}")

                    def register_workspace_created_hook(self, hook_name, callback, priority=100):
                        registered.append(f"wshook:{hook_name}")

                    def register_prompt_section(self, name, after, provider, **kwargs):
                        registered.append(f"prompt:{name}")

                module.plugin.register(FakeApi())
                self.assertNotIn(
                    "skills",
                    registered,
                    "no skills dir: skill registration must be skipped",
                )
                self.assertIn(
                    "slash:cad-setup",
                    registered,
                    "the preflight command registers regardless of skills",
                )
                self.assertNotIn(
                    "prompt:text_to_cad_setup_hint",
                    registered,
                    "no skills: the setup hint must not advertise them",
                )
            finally:
                sys.modules.pop("_test_qwenpaw_plugin_orphan", None)


def install_agentscope_stub() -> dict[str, types.ModuleType]:
    """A minimal `agentscope.message`: Msg and TextBlock carry their kwargs."""
    message = types.ModuleType("agentscope.message")

    class Msg:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class TextBlock:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    message.Msg = Msg
    message.TextBlock = TextBlock
    return {
        "agentscope": types.ModuleType("agentscope"),
        "agentscope.message": message,
    }


def install_store_stub(manifest_path: Path) -> dict[str, types.ModuleType]:
    """The qwenpaw skill_system.store surface the fabrication gate imports.

    `mutate_json` behaves like the real one: read the manifest (or the default
    when absent), apply the mutation, write it back.
    """
    store = types.ModuleType("qwenpaw.agents.skill_system.store")
    store.default_workspace_manifest = lambda: {"skills": {}}
    store.get_workspace_skill_manifest_path = lambda workspace_dir: manifest_path

    def mutate_json(path, default, mutate):
        payload = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
        )
        path.write_text(json.dumps(mutate(payload)), encoding="utf-8")

    store.mutate_json = mutate_json
    modules = {
        name: types.ModuleType(name)
        for name in ("qwenpaw", "qwenpaw.agents", "qwenpaw.agents.skill_system")
    }
    modules["qwenpaw.agents.skill_system.store"] = store
    return modules


class CadSetupDoctorTest(unittest.TestCase):
    """`cadgen doctor` writes its failures to stderr; /cad-setup must show them.

    On a pin mismatch stdout still carries the healthy half (`kernel OK`), so
    preferring stdout hides the mismatch and its repair instructions — the
    report review reproduced.
    """

    def run_doctor(self, module, **kwargs):
        import subprocess

        completed = subprocess.CompletedProcess(args=[], **kwargs)
        with mock.patch("subprocess.run", return_value=completed):
            return module._run_cadgen_doctor(Path("skills/cad"), "0.7.15")

    def test_mismatch_reports_stderr_with_the_repair_instructions(self) -> None:
        module = load_entry(self)
        status, detail = self.run_doctor(
            module,
            returncode=3,
            stdout="cadgen 0.7.14\n  kernel   OK — OCP at /x\n",
            stderr=(
                "  pin      MISMATCH — skills/cad pins cadgen==0.7.15, "
                "but cadgen 0.7.14 is installed.\n"
                "This is not the installation the skill uses. Run cadgen "
                "with its launch command:\n"
                "  uvx --from cadgen==0.7.15 cadgen ...\n"
            ),
        )
        self.assertEqual(status, "mismatch")
        self.assertIn("MISMATCH", detail)
        self.assertIn("launch command", detail)
        self.assertNotIn("kernel   OK", detail)

    def test_kernel_load_failure_reports_stderr(self) -> None:
        module = load_entry(self)
        status, detail = self.run_doctor(
            module,
            returncode=4,
            stdout="cadgen 0.7.15\n  pin      OK — cadgen==0.7.15\n",
            stderr="  kernel   FAILED — ImportError: DLL load failed while importing OCP\n",
        )
        self.assertEqual(status, "error")
        self.assertIn("kernel   FAILED", detail)
        self.assertIn("ImportError", detail)

    def test_success_reports_the_stdout_tail(self) -> None:
        module = load_entry(self)
        status, detail = self.run_doctor(
            module,
            returncode=0,
            stdout="cadgen 0.7.15\n  kernel   OK — OCP at /x\n  pin      OK — cadgen==0.7.15\n",
            stderr="",
        )
        self.assertEqual(status, "ok")
        self.assertIn("pin      OK", detail)

    def test_report_line_drops_doctor_s_own_indentation(self) -> None:
        # Doctor indents its report lines; the skill line already indents, so
        # a detail pasted as-is reads as a gap ("cad: OK —   pin   OK").
        module = load_entry(self)
        self.assertEqual(
            module._report_detail(
                "  pin      MISMATCH — skills/cad pins cadgen==0.7.15.\n"
                "This is not the installation the skill uses.\n"
            ),
            "pin      MISMATCH — skills/cad pins cadgen==0.7.15.\n"
            "      This is not the installation the skill uses.",
        )

    def test_uv_progress_written_before_the_report_is_dropped(self) -> None:
        # Measured live: with the pinned runtime uncached, uv writes its
        # download and install lines to the same stderr, ahead of doctor.
        module = load_entry(self)
        status, detail = self.run_doctor(
            module,
            returncode=3,
            stdout="cadgen 0.7.14\n  kernel   OK — OCP at /x\n",
            stderr=(
                "Downloading cadgen (11.0MiB)\nDownloaded cadgen\n"
                "Installed 59 packages in 68ms\n"
                "  pin      MISMATCH — skills/cad/SKILL.md pins cadgen==0.7.15, "
                "but cadgen 0.7.14 is installed.\n"
            ),
        )
        self.assertEqual(status, "mismatch")
        self.assertTrue(detail.lstrip().startswith("pin"), detail)
        self.assertNotIn("Installed 59 packages", detail)

    def test_a_failure_with_no_doctor_report_line_is_kept_whole(self) -> None:
        # No `pin`/`kernel` anchor means the trimmer did not recognise the
        # failure (uv itself refused to run cadgen): show it all, not nothing.
        module = load_entry(self)
        status, detail = self.run_doctor(
            module,
            returncode=1,
            stdout="",
            stderr="error: Failed to spawn: `cadgen`\n  Caused by: no such file\n",
        )
        self.assertEqual(status, "error")
        self.assertIn("Failed to spawn", detail)
        self.assertIn("no such file", detail)


class CadSetupHandlerTest(unittest.TestCase):
    """QwenPaw awaits the handler on its event loop: no subprocess may run there.

    A doctor run can take minutes while uv downloads the pinned runtime, so a
    blocking call stalls every other request the app serves.
    """

    def test_subprocesses_run_off_the_event_loop_thread(self) -> None:
        import asyncio
        import subprocess
        import threading

        module = load_entry(self)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for name in ("cad", "dxf"):
                skill = workspace / "skills" / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    "- `cadgen` below means `uvx --no-config --managed-python "
                    "--python 3.13 --from cadgen==0.7.15 cadgen`\n",
                    encoding="utf-8",
                )

            main_thread = threading.get_ident()
            calls: list[tuple[int, list[str]]] = []

            def fake_run(argv, **kwargs):
                calls.append((threading.get_ident(), list(argv)))
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="ok\n", stderr=""
                )

            ctx = types.SimpleNamespace(workspace_dir=str(workspace))
            with (
                mock.patch("shutil.which", return_value="/usr/bin/uvx"),
                mock.patch("subprocess.run", side_effect=fake_run),
                mock.patch.dict(sys.modules, install_agentscope_stub()),
            ):
                msg = asyncio.run(module._cad_setup_handler(ctx, ""))

        self.assertTrue(calls, "expected doctor, viewer and daemon subprocesses")
        offenders = [argv for tid, argv in calls if tid == main_thread]
        self.assertEqual(
            offenders,
            [],
            "a subprocess ran on the event loop thread",
        )
        text = msg.content[0].text
        self.assertIn("cad: OK", text)
        # One live check per distinct pin: dxf shares cad's.
        self.assertIn("dxf: cadgen==0.7.15 (checked with cad)", text)
        self.assertIn("viewer:", text)
        self.assertIn("daemon:", text)

    def test_uv_missing_marks_skills_unchecked_without_failing(self) -> None:
        import asyncio

        module = load_entry(self)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            skill = workspace / "skills" / "cad"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "`uvx --no-config --managed-python --python 3.13 "
                "--from cadgen==0.7.15 cadgen`\n",
                encoding="utf-8",
            )
            ctx = types.SimpleNamespace(workspace_dir=str(workspace))
            with (
                mock.patch("shutil.which", return_value=None),
                mock.patch.dict(sys.modules, install_agentscope_stub()),
            ):
                msg = asyncio.run(module._cad_setup_handler(ctx, ""))
        text = msg.content[0].text
        self.assertIn("uv: NOT INSTALLED", text)
        self.assertIn("cad: cadgen==0.7.15 — UNCHECKED", text)


class FabricationGateTest(unittest.TestCase):
    """The once-only marker is written only once the gate has actually applied.

    The host catches an install failure and still runs the gate hook, so an
    entry missing from the manifest is one the install never wrote. Marking
    anyway leaves the fabrication skills enabled by the next startup's
    successful install with the gate skipped — the sequence review reproduced.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="qwenpaw-gate-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.module = load_entry(self)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.skills_dir = root / "skills"
        for name in self.module.FABRICATION_SKILLS:
            skill = self.skills_dir / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
        self.manifest_path = self.workspace / "skill_manifest.json"

    def gate(self):
        with mock.patch.dict(
            sys.modules, install_store_stub(self.manifest_path)
        ):
            self.module._apply_fabrication_gate(self.workspace, self.skills_dir)

    def write_manifest(self, skills: dict) -> None:
        self.manifest_path.write_text(json.dumps({"skills": skills}), encoding="utf-8")

    def read_manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))["skills"]

    def enabled_entries(self) -> dict:
        return {
            name: {"source": f"plugin:cad/{name}", "enabled": True}
            for name in self.module.FABRICATION_SKILLS
        }

    def marker(self) -> Path:
        return self.workspace / self.module._PROVISION_MARKER

    def test_missing_entries_leave_the_gate_pending(self) -> None:
        self.write_manifest({})
        with self.assertLogs(self.module.logger, level="WARNING") as logs:
            self.gate()
        self.assertFalse(
            self.marker().exists(),
            "no gated entry was installed, so the workspace is not provisioned",
        )
        self.assertIn("still pending", " ".join(logs.output))

    def test_failed_install_then_successful_retry_still_gates(self) -> None:
        # First startup: the install hook failed, the manifest has no entries.
        self.write_manifest({})
        self.gate()
        self.assertFalse(self.marker().exists())
        # Second startup: the install succeeded. The gate must still apply.
        self.write_manifest(self.enabled_entries())
        self.gate()
        self.assertTrue(self.marker().exists())
        for name, entry in self.read_manifest().items():
            self.assertFalse(entry["enabled"], f"{name} was left enabled")

    def test_existing_marker_leaves_user_choices_alone(self) -> None:
        self.marker().touch()
        self.write_manifest(self.enabled_entries())
        self.gate()
        for name, entry in self.read_manifest().items():
            self.assertTrue(
                entry["enabled"], f"{name}: the user's own choice must win"
            )


if __name__ == "__main__":
    unittest.main()
# -*- coding: utf-8 -*-
"""text-to-cad QwenPaw plugin entry point.

Registers the repository's CAD, robotics, and fabrication skills with every
QwenPaw workspace, plus a ``/cad-setup`` preflight command that reports the
cadgen runtime state (presence, per-skill pin, viewer/daemon) without the
agent having to read a skill first.

The plugin ships no tools of its own: the skills are instructions over the
``cadgen`` distribution, which each skill's SKILL.md runs through the one
pinned launch command (``uvx ... --from cadgen==<version> cadgen``).

This module must stay importable without QwenPaw installed (stdlib only at
module scope; qwenpaw/agentscope imports sit inside functions): QwenPaw
validates a plugin by importing it and requiring a ``plugin`` instance, and
this repository's policy test (``tests/python/global/test_qwenpaw_plugin.py``)
runs the same import on a checkout that has no ``qwenpaw`` package.

Skills directory resolution
---------------------------

The QwenPaw loader installs a plugin by copying THIS directory
(``.qwenpaw-plugin/``) into ``~/.qwenpaw/plugins/<id>/``, so nothing outside it
survives the install. Rather than commit a second copy of ``skills/`` to the
repository, the plugin resolves a tree that already exists, in this order:

1. ``plugins.cad.skills_dir`` in QwenPaw's own config, handed to the plugin as
   ``api.config``. Point it at a checkout's ``skills/`` and the install follows
   that tree live, the way the Claude, Codex and Skills-CLI surfaces do.
2. ``<plugin_dir>/skills`` -- a copy generated on demand with the rsync recipe
   in this directory's README, for an install with no checkout to point at.
3. ``<plugin_dir>/../skills`` -- the sibling a checkout provides, so the plugin
   also works when it is run in place rather than installed.

A configured path that does not resolve is reported as an error and is NOT
silently replaced by a lower-priority candidate: an outdated path should be
loud, not shadowed by a stale copy. ``/cad-setup`` prints which tree won.

If nothing resolves, the plugin logs a clear error and skips skill registration
instead of raising: a broken path must degrade to "plugin without skills", not
fail the QwenPaw app startup.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type hints only, never evaluated
    from qwenpaw.plugins.api import PluginApi

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent

#: QwenPaw hands each plugin the ``plugins.<id>`` section of its own config as
#: ``api.config``; this key names a skills tree to provision from, which beats
#: any copy and is what lets an install follow one source of truth.
CONFIG_SKILLS_KEY = "skills_dir"

#: What the last ``register()`` resolved, reported by /cad-setup. A plugin with
#: no reachable skills tree still registers that command, because it is the
#: thing the user runs once skills are missing.
_SKILLS_STATE: dict[str, str] = {"resolved": "not registered", "path": ""}

#: Skills the fabrication/handoff boundary applies to. These are enabled by
#: default everywhere else in this repo; under QwenPaw they start disabled and
#: the user opts in per workspace, because they reach real machines: starting
#: prints on a LAN printer (bambu-labs) and uploading parts for manufacture
#: (sendcutsend). Every other skill is analysis, authoring, or local-only.
FABRICATION_SKILLS = ("bambu-labs", "sendcutsend")

#: Marker written into a workspace the first time the plugin provisions it.
#: Keeps the once-only disable semantics: after the first provision the user's
#: own enable/disable choices win, on every later startup.
_PROVISION_MARKER = ".text-to-cad-provisioned"

#: One-line pointer injected into the system prompt after the workspace
#: section. Short on purpose: it costs tokens on every turn.
_PROMPT_HINT = (
    "text-to-cad skills (CAD/URDF/SDF/DXF/G-code) are installed; "
    "run /cad-setup once to verify the cadgen runtime before first use."
)


def _is_skills_tree(candidate: Path) -> bool:
    """True when *candidate* holds this library's skills (``cad`` is the canary)."""
    return candidate.is_dir() and (candidate / "cad" / "SKILL.md").is_file()


def _resolve_skills_dir(config: dict | None = None) -> Path | None:
    """Return the skills directory the plugin will provision from, or None.

    Records which candidate won in ``_SKILLS_STATE`` so ``/cad-setup`` can
    report it: the caller has no way to tell a configured tree from a copy.

    A configured path that does not resolve is reported and returns None rather
    than falling through: a stale pointer is a misconfiguration to fix, and a
    silent fallback to a copy would let it serve skills nobody updated.
    """
    configured = (config or {}).get(CONFIG_SKILLS_KEY)
    if isinstance(configured, str) and configured.strip():
        candidate = Path(configured.strip()).expanduser()
        if _is_skills_tree(candidate):
            _SKILLS_STATE.update(resolved="configured", path=str(candidate))
            return candidate
        logger.error(
            "text-to-cad: %s=%s is not a skills tree (no cad/SKILL.md inside). "
            "Point it at a text-to-cad checkout's skills/ directory, or drop "
            "the key to fall back to a bundled copy.",
            CONFIG_SKILLS_KEY,
            candidate,
        )
        _SKILLS_STATE.update(
            resolved="configured path invalid", path=str(candidate)
        )
        return None

    for label, candidate in (
        ("bundled copy", PLUGIN_DIR / "skills"),
        ("checkout sibling", PLUGIN_DIR.parent / "skills"),
    ):
        if _is_skills_tree(candidate):
            _SKILLS_STATE.update(resolved=label, path=str(candidate))
            return candidate

    _SKILLS_STATE.update(resolved="nothing reachable", path="")
    return None


def _workspace_dirs() -> list[Path]:
    """Every configured workspace directory (empty list when unavailable)."""
    try:
        from qwenpaw.agents.skill_system.registry import list_workspaces

        return [Path(w["workspace_dir"]) for w in list_workspaces()]
    except Exception as exc:  # noqa: BLE001 - provisioning is best-effort
        logger.warning("text-to-cad: cannot list workspaces: %s", exc)
        return []


def _apply_fabrication_gate(workspace_dir: Path, skills_dir: Path) -> None:
    """Disable the fabrication skills the first time a workspace is provisioned.

    Runs after the host's install hook (priority 85 > 80). The marker file
    makes it exactly-once per provision cycle: on later startups the user's
    own enable/disable choices are left alone. The marker is removed again
    by the plugin's uninstall hook, so a reinstall treats the workspace as
    freshly provisioned and re-applies the gate (the uninstall removes the
    plugin-sourced skills; without this the reinstall would re-enable them
    and the stale marker would keep the gate off).
    """
    marker = workspace_dir / _PROVISION_MARKER
    if marker.exists():
        return
    try:
        from qwenpaw.agents.skill_system.store import (
            default_workspace_manifest,
            get_workspace_skill_manifest_path,
            mutate_json,
        )

        installed = {
            d.name
            for d in skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        }
        gated = sorted(installed & set(FABRICATION_SKILLS))
        if not gated:
            marker.touch()
            return

        def _disable(payload: dict) -> dict:
            skills = payload.setdefault("skills", {})
            for name in gated:
                entry = skills.get(name)
                if entry is None:
                    continue
                if str(entry.get("source", "")).startswith("plugin:cad"):
                    entry["enabled"] = False
            return payload

        manifest_path = get_workspace_skill_manifest_path(workspace_dir)
        mutate_json(manifest_path, default_workspace_manifest(), _disable)

        # The marker is exactly-once only if the gate actually applied: the
        # host catches an install failure and still runs this hook, and an
        # entry missing here is one the install hook never wrote. Marking
        # anyway would leave the fabrication skills enabled by the next
        # startup's successful install, with the gate skipped forever. Stay
        # pending until every gated entry exists and reads back disabled.
        skills = json.loads(manifest_path.read_text(encoding="utf-8")).get("skills", {})
        pending = [
            name
            for name in gated
            if not (
                isinstance(skills.get(name), dict)
                and skills[name].get("enabled") is False
            )
        ]
        if pending:
            logger.warning(
                "text-to-cad: fabrication gate still pending in %s: %s not "
                "confirmed disabled — will retry on the next startup",
                workspace_dir.name,
                ", ".join(pending),
            )
            return
        marker.touch()
        logger.info(
            "text-to-cad: fabrication skills left disabled in %s: %s "
            "(enable per workspace when the user opts in)",
            workspace_dir.name,
            ", ".join(gated),
        )
    except Exception as exc:  # noqa: BLE001 - never block startup
        logger.warning(
            "text-to-cad: fabrication gate skipped for %s: %s",
            workspace_dir.name,
            exc,
        )


def _skill_launch_pin(skill_md: Path) -> str | None:
    """The cadgen version a skill's launch command pins in its SKILL.md, or None."""
    match = re.search(
        r"--from\s+cadgen==([0-9]+\.[0-9]+\.[0-9]+)",
        skill_md.read_text(encoding="utf-8"),
    )
    return match.group(1) if match else None


def _launch_argv(pin: str, *argv: str) -> list[str]:
    """The skills' one launch command for a cadgen verb, pinned.

    Mirrors ``cadgen._internal.launch.LAUNCHER``; it cannot come from cadgen
    itself, because checking whether cadgen runs is exactly what this is for.
    """
    return [
        "uvx", "--no-config", "--managed-python", "--python", "3.13",
        "--from", f"cadgen=={pin}", "cadgen", *argv,
    ]


def _doctor_report(text: str) -> str:
    """Doctor's own lines out of a stderr block uv may have preceded.

    When the pinned runtime is not cached yet, uv writes its download and
    install progress to the same pipe, ahead of doctor's report. The user
    needs the report, which starts at a `pin` or `kernel` line. Text with no
    such line comes back unchanged: an unexpected failure is shown whole
    rather than trimmed away.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s*(?:pin|kernel)\s+\S", line):
            return "\n".join(lines[index:])
    return text


def _run_cadgen_doctor(skill_dir: Path, pin: str) -> tuple[str, str]:
    """Run ``cadgen doctor <skill_dir>`` through the pinned launch command.

    Returns (status, detail): status is "ok" (exit 0), "mismatch" (exit 3) or
    "error". On a failure the detail is doctor's stderr, which is where the
    version mismatch, its repair instructions and kernel-load errors go;
    stdout still reports the healthy half (``kernel OK``), so choosing it
    first would hide the failure's explanation.
    """
    import subprocess

    try:
        result = subprocess.run(
            _launch_argv(pin, "doctor", str(skill_dir)),
            capture_output=True,
            text=True,
            timeout=600,  # the first run may download the pinned runtime
        )
    except subprocess.TimeoutExpired:
        return ("error", "cadgen doctor timed out after 600s")
    except OSError as exc:
        return ("error", f"cadgen doctor failed to run: {exc}")

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode == 0:
        tail = stdout.splitlines()
        return ("ok", tail[-1] if tail else "")
    if result.returncode == 3:
        return ("mismatch", _doctor_report(stderr) or (stdout.splitlines()[-1] if stdout else ""))
    detail = _doctor_report(stderr) or (stdout.splitlines()[-1] if stdout else "")
    return ("error", detail or f"cadgen doctor exited {result.returncode}")


def _run_status(argv: list[str]) -> str:
    """The first stdout line of a lifecycle query, or why it could not run."""
    import subprocess

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"status unavailable ({exc})"
    out = (result.stdout or "").strip()
    return out.splitlines()[0] if out else "none"


def _report_detail(detail: str) -> str:
    """Doctor's detail on one report line, its own indentation dropped.

    Doctor indents its report lines (`  pin      OK — ...`); the skill line
    already indents, so the two stack up as a gap. Continuation lines of a
    multi-line failure block stay under the line they belong to.
    """
    lines = detail.splitlines()
    if not lines:
        return ""
    return lines[0].strip() + "".join(
        f"\n      {line.strip()}" for line in lines[1:] if line.strip()
    )


async def _cad_setup_handler(ctx, args: str):
    """/cad-setup — report the cadgen runtime state for this workspace.

    Checks, in order: uv (which the skills' one launch command runs through),
    each cadgen-pinned skill's launch-command pin (verified with
    ``cadgen doctor``), and the viewer/daemon lifecycle so stray background
    processes are visible. QwenPaw awaits this handler on its event loop, so
    every subprocess runs on a worker thread: a doctor run can take minutes
    while uv downloads the pinned runtime, and a blocking call would stall
    the app's other requests for all of it. Returns a Msg; never raises into
    the dispatcher.
    """
    import asyncio
    import shutil

    from agentscope.message import Msg, TextBlock

    workspace_dir = getattr(ctx, "workspace_dir", None)
    lines: list[str] = ["text-to-cad runtime check:"]

    if workspace_dir is None:
        lines.append("  workspace: unknown (no workspace_dir on context)")
        skills_root: Path | None = None
    else:
        workspace_dir = Path(workspace_dir)
        skills_root = workspace_dir / "skills"
        lines.append(f"  workspace: {workspace_dir}")

    source = _SKILLS_STATE["resolved"]
    if _SKILLS_STATE["path"]:
        source = f"{source} ({_SKILLS_STATE['path']})"
    lines.append(f"  skills source: {source}")

    # 1. uv: the skills run cadgen as `uvx ... --from cadgen==<pin> cadgen`,
    # so the runtime prerequisite is uv, not a cadgen on PATH.
    uvx = shutil.which("uvx")
    if uvx is None:
        lines.append(
            "  uv: NOT INSTALLED — the skills run cadgen through uv; "
            "install it (https://docs.astral.sh/uv/)"
        )

    # 2. The launch-command pin of every cadgen-pinned skill in the workspace.
    # The launch command resolves exactly the pinned installation, so one live
    # doctor run covers every skill sharing a pin.
    pinned: dict[str, str] = {}
    if skills_root is not None and skills_root.is_dir():
        for skill_dir in sorted(skills_root.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            pin = _skill_launch_pin(skill_md)
            if pin is not None:
                pinned[skill_dir.name] = pin

    if skills_root is None or not skills_root.is_dir():
        lines.append("  skills: no skills/ directory in this workspace")
    elif not pinned:
        lines.append("  skills: no cadgen-pinned skills in this workspace")
    else:
        first_by_pin: dict[str, str] = {}
        for name, pin in pinned.items():
            first_by_pin.setdefault(pin, name)
        if len(first_by_pin) > 1:
            lines.append(
                "  pins: INCONSISTENT — skills pin different cadgen versions "
                "(a partial update?); each pin is a separate installation"
            )
        for name, pin in pinned.items():
            checked_with = first_by_pin[pin]
            if checked_with != name:
                lines.append(f"  {name}: cadgen=={pin} (checked with {checked_with})")
                continue
            if uvx is None:
                lines.append(f"  {name}: cadgen=={pin} — UNCHECKED (uv not installed)")
                continue
            status, detail = await asyncio.to_thread(
                _run_cadgen_doctor, skills_root / name, pin
            )
            mark = {"ok": "OK", "mismatch": "MISMATCH", "error": "ERROR"}[status]
            lines.append(f"  {name}: {mark} — {_report_detail(detail)}")

    # 3. Background lifecycle visibility (viewer instances, warm daemon),
    # through the same pinned launch command.
    if uvx is not None and pinned:
        pin = next(iter(pinned.values()))
        for label, verb in (
            ("viewer", ("viewer", "list", "--json")),
            ("daemon", ("daemon", "status")),
        ):
            out = await asyncio.to_thread(_run_status, _launch_argv(pin, *verb))
            lines.append(f"  {label}: {out}")

    return Msg(
        name="system",
        role="assistant",
        content=[TextBlock(type="text", text="\n".join(lines))],
    )


class TextToCadPlugin:
    """Installs the packaged skills into every QwenPaw workspace."""

    def register(self, api: PluginApi) -> None:
        """Register skills, the preflight command, and the setup hint.

        Args:
            api: PluginApi instance provided by the QwenPaw loader.
        """
        skills_dir = _resolve_skills_dir(getattr(api, "config", None))
        if skills_dir is None:
            logger.error(
                "text-to-cad: no skills/ tree reachable from %s — skill "
                "registration skipped. Either set %s in this plugin's config "
                "to a text-to-cad checkout's skills/ directory, or generate "
                "the bundled copy with the rsync recipe in .qwenpaw-plugin/"
                "README.md. Run /cad-setup to see what was looked for.",
                PLUGIN_DIR,
                CONFIG_SKILLS_KEY,
            )
        else:
            logger.info("Registering text-to-cad skills (%s)...", skills_dir)
            api.register_skill_provider(
                skills_dir=skills_dir,
                enabled_by_default=True,
                channels=["all"],
            )

            def _gate(workspace_info: dict) -> None:
                workspace_dir = Path(workspace_info.get("workspace_dir", ""))
                if workspace_dir.is_dir():
                    _apply_fabrication_gate(workspace_dir, skills_dir)

            api.register_startup_hook(
                hook_name="text_to_cad_fabrication_gate",
                callback=lambda: [
                    _gate({"workspace_dir": str(w)}) for w in _workspace_dirs()
                ],
                priority=85,  # after the host's install hook (priority 80)
            )
            api.register_workspace_created_hook(
                hook_name="text_to_cad_fabrication_gate",
                callback=_gate,
                priority=85,
            )

            def _clear_markers(plugin_id: str, delete_files: bool = False) -> None:
                """Remove the provision markers so a reinstall re-gates.

                The host uninstall deletes the plugin-sourced skills but not
                this plugin's marker files; without this hook a reinstall
                would find the stale marker, skip the gate, and leave the
                fabrication skills re-enabled by the fresh install.
                """
                _ = delete_files  # part of the uninstall hook contract
                for workspace_dir in _workspace_dirs():
                    try:
                        (workspace_dir / _PROVISION_MARKER).unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "text-to-cad: could not clear provision marker "
                            "in %s: %s",
                            workspace_dir.name,
                            exc,
                        )

            api.register_uninstall_hook(
                hook_name="text_to_cad_clear_provision_markers",
                callback=_clear_markers,
            )
            logger.info("✓ text-to-cad skills registered from %s", skills_dir)

        # /cad-setup preflight: the first-run failure mode for every skill is
        # "cadgen missing or pinned elsewhere"; the command surfaces that (and
        # any viewer/daemon strays) before the agent burns a turn on it.
        api.register_slash_command(
            name="cad-setup",
            handler=_cad_setup_handler,
            category="plugin",
            help_text=(
                "Verify the text-to-cad runtime: cadgen presence/version, "
                "per-skill cadgen pins, viewer and daemon status"
            ),
        )

        # One-line system-prompt hint so the agent knows the preflight exists
        # without reading any skill. Registered only when skills resolved; a
        # plugin without skills should not advertise them.
        if skills_dir is not None:
            api.register_prompt_section(
                name="text_to_cad_setup_hint",
                after="workspace",
                provider=lambda agent: _PROMPT_HINT,
            )

        logger.info("✓ text-to-cad plugin registered")


# Export plugin instance (required by the QwenPaw plugin loader).
plugin = TextToCadPlugin()

"""Runs a tenant's stack with Pulumi's Automation API.

The task has no route to the internet, so nothing is downloaded at run time: the Pulumi CLI
and the AWS provider plugin are in the image, state is kept in the platform's state bucket
(through the S3 gateway endpoint) and secrets are encrypted with its KMS key. Stack
`assay-tenant/<tenant_id>`: project `assay-tenant`, stack name the tenant ID.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulumi import automation as auto

from assay_tss.provision import program
from assay_tss.provision.settings import PROJECT, ProvisionSettings

log = logging.getLogger(__name__)

# Never reach for the network: no update check, no plugin download.
CLI_ENVIRONMENT = {
    "PULUMI_SKIP_UPDATE_CHECK": "true",
    "PULUMI_DISABLE_AUTOMATIC_PLUGIN_ACQUISITION": "true",
    "PULUMI_AUTOMATION_API_SKIP_VERSION_CHECK": "true",
    "NO_COLOR": "1",
}
MAX_ERROR = 500
_ERROR_LINE = re.compile(r"^\s*(?:error|Error):\s*(.+)$", re.MULTILINE)


class StackError(RuntimeError):
    """The tenant's stack did not converge. The message is short and safe to record."""


@dataclass(frozen=True)
class PlannedChange:
    """One resource a preview would change. `diffs` names the changed top-level inputs."""

    urn: str
    name: str
    type: str
    op: str
    diffs: tuple[str, ...] = ()


# Preview steps that change nothing.
UNCHANGED_OPS = frozenset({"same", "read"})


@dataclass(frozen=True)
class StackOutputs:
    bucket: str
    kms_key_arn: str
    vector_bucket: str
    vector_index: str
    face_collection: str


def summarise(error: BaseException) -> str:
    """The first error lines of a Pulumi failure, without the full engine output."""
    text = str(error)
    lines = [m.group(1).strip() for m in _ERROR_LINE.finditer(text)]
    summary = "; ".join(dict.fromkeys(lines)) if lines else text.strip().splitlines()[-1:]
    if isinstance(summary, list):
        summary = summary[0] if summary else type(error).__name__
    return summary[:MAX_ERROR]


def prepare_pulumi_home(environ: Mapping[str, str]) -> None:
    """Make Pulumi's writable directories, and point its plugin directory at the image's.

    The task's root filesystem is read-only. Pulumi writes workspace files under
    PULUMI_HOME and temporary files under TMPDIR, both on the task's writable volume; the
    provider plugin stays where the image put it, read-only.
    """
    for name in ("TMPDIR", "PULUMI_HOME"):
        if environ.get(name):
            Path(environ[name]).mkdir(parents=True, exist_ok=True)
    home, plugins = environ.get("PULUMI_HOME"), environ.get("PULUMI_PLUGINS_DIR")
    if home and plugins:
        link = Path(home) / "plugins"
        if not link.exists():
            link.symlink_to(plugins, target_is_directory=True)


class StackRunner:
    def __init__(self, settings: ProvisionSettings) -> None:
        self._settings = settings

    def _stack(self, tenant_id: uuid.UUID) -> auto.Stack:
        s = self._settings
        stack_name = str(tenant_id)
        work_dir = s.work_dir / "stacks" / stack_name
        work_dir.mkdir(parents=True, exist_ok=True)
        return auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=PROJECT,
            program=program.tenant_program(s, tenant_id),
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=PROJECT,
                    runtime="python",
                    backend=auto.ProjectBackend(url=s.backend),
                ),
                secrets_provider=s.secrets,
                stack_settings={stack_name: auto.StackSettings(secrets_provider=s.secrets)},
                env_vars=dict(CLI_ENVIRONMENT),
            ),
        )

    @staticmethod
    def _log_line(line: str) -> None:
        if line.strip():
            log.info("pulumi", extra={"line": line.rstrip()})

    def up(self, tenant_id: uuid.UUID, *, recover: bool = False) -> StackOutputs:
        """Create or converge the tenant's resources. Safe to repeat.

        `recover` is for a retry after a run that may have died part-way: it drops a stale
        lock and clears operations that never finished before converging again.
        """
        try:
            stack = self._stack(tenant_id)
            if recover:
                try:
                    stack.cancel()
                except auto.CommandError:
                    log.info("no update to cancel")
                stack.refresh(on_output=self._log_line, clear_pending_creates=True, color="never")
            result = stack.up(on_output=self._log_line, color="never")
        except auto.CommandError as exc:
            raise StackError(summarise(exc)) from exc
        return self._outputs(result.outputs)

    def preview(self, tenant_id: uuid.UUID) -> dict[str, int]:
        """What `up` would change, by operation. Used by the image test."""
        try:
            result = self._stack(tenant_id).preview(color="never")
        except auto.CommandError as exc:
            raise StackError(summarise(exc)) from exc
        return {str(op): count for op, count in result.change_summary.items()}

    def plan(self, tenant_id: uuid.UUID) -> list[PlannedChange]:
        """Every change `up` would make, resource by resource (a preview: changes nothing)."""
        changes: list[PlannedChange] = []

        def collect(event: auto.EngineEvent) -> None:
            pre = event.resource_pre_event
            if pre is None:
                return
            meta = pre.metadata
            op = str(meta.op.value if hasattr(meta.op, "value") else meta.op)
            if op in UNCHANGED_OPS:
                return
            changes.append(
                PlannedChange(
                    urn=meta.urn,
                    name=meta.urn.rsplit("::", 1)[-1],
                    type=meta.type,
                    op=op,
                    diffs=tuple(sorted(meta.diffs or ())),
                )
            )

        try:
            self._stack(tenant_id).preview(color="never", on_event=collect)
        except auto.CommandError as exc:
            raise StackError(summarise(exc)) from exc
        return changes

    def apply(self, tenant_id: uuid.UUID, urns: Sequence[str]) -> StackOutputs:
        """`up` limited to these resources (`--target`): nothing else can change."""
        try:
            stack = self._stack(tenant_id)
            stack.up(on_output=self._log_line, color="never", target=list(urns))
            outputs = stack.outputs()
        except auto.CommandError as exc:
            raise StackError(summarise(exc)) from exc
        return self._outputs(outputs)

    @staticmethod
    def _outputs(outputs: Mapping[str, Any]) -> StackOutputs:
        missing = [name for name in program.OUTPUTS if name not in outputs]
        if missing:
            raise StackError(f"stack has no output {', '.join(missing)}")
        values = {name: str(outputs[name].value) for name in program.OUTPUTS}
        return StackOutputs(**values)

"""The built-in general pack (DESIGN.md section 17.1).

Contribution kinds: ``general.work`` (any files outside reserved paths) and
``general.report`` (Markdown under ``reports/``). Policies:

- ``general.review@1`` (default): kind rules, then independent review.
- ``general.json@1``: JSON Schema from the contract's ``payload.output_schema``.
- ``general.command@1``: an operator-defined check from ``[checks.<name>]``, run
  over the candidate tree; the same check runs as the final check.

Only the operator defines commands, so a contract cannot make the Hub run anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema

from geyser.core.models import TaskContract, ValidationReport, Verdict
from geyser.domains.base import (
    AgentView,
    CandidateView,
    ContributionKindSpec,
    GuideSet,
    InstanceContext,
    OperationContext,
    OperationSpec,
    PolicySpec,
    Readiness,
    TaskKindSpec,
    TreeSnapshot,
    ValidationContext,
)

_GUIDE_DIR = Path(__file__).parent

REVIEW = "general.review@1"
JSON = "general.json@1"
COMMAND = "general.command@1"

_OUTPUT_TAIL = 6_000


class GeneralPack:
    name = "general"

    # ------------------------------------------------------------- vocabulary
    def task_kinds(self) -> Mapping[str, TaskKindSpec]:
        return {
            "work": TaskKindSpec("work", "Produce, verify, correct or publish files."),
            "planning": TaskKindSpec("planning", "Decompose a goal into published subtasks."),
        }

    def contribution_kinds(self) -> Mapping[str, ContributionKindSpec]:
        return {
            "general.work": ContributionKindSpec(
                name="general.work",
                description="Any files outside the reserved paths.",
                default_policy=REVIEW,
            ),
            "general.report": ContributionKindSpec(
                name="general.report",
                description="Markdown under reports/: findings, decisions, investigations.",
                default_policy=REVIEW,
                allowed_prefixes=("reports/",),
                companions=_markdown_only,
            ),
        }

    def policies(self) -> Mapping[str, PolicySpec]:
        return {
            REVIEW: PolicySpec(REVIEW, validator=None, final_check=None,
                               review_guide="general-review"),
            JSON: PolicySpec(JSON, validator="json_schema", final_check=None,
                             review_guide="general-review"),
            COMMAND: PolicySpec(COMMAND, validator="command", final_check="command",
                                review_guide="general-review"),
        }

    def default_policy(self) -> str:
        return REVIEW

    # ----------------------------------------------------------------- checks
    def validate_contract(self, contract: TaskContract) -> None:
        if contract.policy == JSON:
            schema = contract.payload.get("output_schema")
            if not isinstance(schema, dict):
                raise ValueError(f"{JSON} needs `payload.output_schema`, a JSON Schema object")
            if _has_remote_reference(schema):
                raise ValueError("`payload.output_schema` may use local references only")
            jsonschema.validators.validator_for(schema).check_schema(schema)
        if contract.policy == COMMAND:
            check = contract.payload.get("check")
            if check is not None and not isinstance(check, str):
                raise ValueError("`payload.check` names an operator-defined check")

    async def validate_candidate(self, ctx: ValidationContext, candidate: CandidateView
                                 ) -> ValidationReport:
        validator = self.policies()[candidate.policy].validator
        if validator == "json_schema":
            return _validate_json(candidate)
        if validator == "command":
            return await _run_check(ctx, candidate.contract, candidate.tree)
        return ValidationReport(verdict=Verdict.ACCEPTED, summary="Kind rules passed.")

    async def check_tree(self, ctx: ValidationContext, tree: TreeSnapshot,
                         members: Sequence[CandidateView]) -> ValidationReport:
        commands = [member for member in members
                    if self.policies().get(member.policy, None) is not None
                    and self.policies()[member.policy].final_check == "command"]
        if not commands:
            return ValidationReport(verdict=Verdict.ACCEPTED, summary="No final check.")
        reports = []
        for check in sorted({_check_name(ctx, member.contract) for member in commands}):
            report = await _run_named_check(ctx, check, tree.path)
            if not report.unqualified_acceptance:
                return report
            reports.append(report.summary)
        return ValidationReport(verdict=Verdict.ACCEPTED, summary="; ".join(reports))

    # ---------------------------------------------------------- agent-facing
    def guides(self) -> GuideSet:
        review = (_GUIDE_DIR / "general-review.md").read_text(encoding="utf-8")
        work = (_GUIDE_DIR / "general-work.md").read_text(encoding="utf-8")
        return GuideSet(
            required={"worker": [("general-work", work)],
                      "reviewer": [("general-review", review)]},
            on_demand={},
        )

    def operations(self) -> Sequence[OperationSpec]:
        return ()

    async def handle_operation(self, ctx: OperationContext, name: str,
                               arguments: dict[str, Any]) -> dict[str, Any]:
        raise ValueError(f"The general pack has no operation {name!r}.")

    def agent_environment(self, agent: AgentView) -> Mapping[str, str]:
        return {}

    # -------------------------------------------------------------- lifecycle
    async def prepare_instance(self, ctx: InstanceContext) -> Readiness:
        return Readiness(ready=True)

    async def resource_status(self) -> dict[str, Any] | None:
        return None


def _markdown_only(paths: set[str]) -> list[str]:
    return [f"`{path}` is not Markdown" for path in sorted(paths) if not path.endswith(".md")]


def _has_remote_reference(value: Any) -> bool:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            return True
        return any(_has_remote_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_remote_reference(item) for item in value)
    return False


def _validate_json(candidate: CandidateView) -> ValidationReport:
    schema = candidate.contract.payload.get("output_schema")
    if not isinstance(schema, dict):
        return ValidationReport(verdict=Verdict.REJECTED,
                                summary="The task has no `payload.output_schema`.")
    output_file = candidate.contract.payload.get("output_file")
    targets = [output_file] if isinstance(output_file, str) else [
        path for path in candidate.changed if path.endswith(".json")]
    if not targets:
        return ValidationReport(verdict=Verdict.REJECTED,
                                summary="The candidate contains no JSON file to check.")
    validator = jsonschema.validators.validator_for(schema)(schema)
    problems: list[str] = []
    for target in targets:
        path = candidate.tree / Path(*target.split("/"))
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            problems.append(f"{target}: missing")
            continue
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            problems.append(f"{target}: not valid JSON ({error})")
            continue
        for issue in sorted(validator.iter_errors(document), key=lambda e: list(e.path))[:20]:
            location = "/".join(str(part) for part in issue.path) or "(root)"
            problems.append(f"{target} at {location}: {issue.message}")
    if problems:
        return ValidationReport(verdict=Verdict.REJECTED, summary=problems[0],
                                details={"problems": problems})
    return ValidationReport(verdict=Verdict.ACCEPTED,
                            summary=f"{len(targets)} JSON file(s) match the output schema.")


def _check_name(ctx: ValidationContext, contract: TaskContract) -> str:
    named = contract.payload.get("check")
    if isinstance(named, str):
        return named
    if len(ctx.config.checks) == 1:
        return next(iter(ctx.config.checks))
    return "tests"


async def _run_check(ctx: ValidationContext, contract: TaskContract, tree: Path
                     ) -> ValidationReport:
    return await _run_named_check(ctx, _check_name(ctx, contract), tree)


async def _run_named_check(ctx: ValidationContext, name: str, tree: Path) -> ValidationReport:
    """Run an operator-defined check in ``tree``: exit 0 accepts, nonzero rejects."""
    check = ctx.config.checks.get(name)
    if check is None:
        return ValidationReport(verdict=Verdict.NEEDS_OPERATOR,
                                summary=f"No check named `{name}` is configured.")
    executable = shutil.which(check.command[0]) or check.command[0]
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL"}}
    environment["HOME"] = str(ctx.scratch)
    try:
        process = await asyncio.create_subprocess_exec(
            executable, *check.command[1:], cwd=tree, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as error:
        return ValidationReport(verdict=Verdict.UNAVAILABLE,
                                summary=f"The check `{name}` could not start: {error}")
    try:
        output, _ = await asyncio.wait_for(process.communicate(),
                                           timeout=check.timeout_minutes * 60)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ValidationReport(verdict=Verdict.UNAVAILABLE,
                                summary=f"The check `{name}` timed out.")
    text = output.decode("utf-8", "replace")[-_OUTPUT_TAIL:]
    if process.returncode == 0:
        return ValidationReport(verdict=Verdict.ACCEPTED, summary=f"`{name}` passed.",
                                details={"output_tail": text})
    if process.returncode is not None and process.returncode < 0:
        return ValidationReport(verdict=Verdict.UNAVAILABLE,
                                summary=f"`{name}` was killed by signal {-process.returncode}.",
                                details={"output_tail": text})
    return ValidationReport(verdict=Verdict.REJECTED,
                            summary=f"`{name}` failed with exit status {process.returncode}.",
                            details={"output_tail": text})

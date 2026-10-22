"""Recipe: a release agent's ``deploy`` tool that ships only the green commit, once, and asks a
person before production.

    uv run python examples/safety_recipes/deploy.py

A release agent works on release requests ("deploy checkout to staging") from the change-management
system. Behind the guard:

* the commit must be the one CI marked green for the service (evidence from the CI API), not the
  newest commit or one the model picked;
* the service and environment must be those of the release request;
* each commit is deployed at most once per (service, environment): the action key names the
  release request, and the policy's ``once_per`` also blocks a second release request for the same
  commit, after a restart too (the store is a SQLite file);
* production needs an approver: the on-call engineer's callback must return ``True``.

Everything is local: CI, the release requests and the deployer are fakes in memory, and the store
lives in a temporary directory. Nothing is deployed.
"""

from __future__ import annotations

import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from aobs import safety

POLICY = Path(__file__).resolve().parent / "deploy_policy.toml"

RELEASES = {  # release requests, from the change-management system (trusted)
    "REL-7": {"service": "checkout", "environment": "staging"},
    "REL-8": {"service": "checkout", "environment": "production"},
    "REL-9": {"service": "checkout", "environment": "production"},  # a second request for the same release
    "REL-10": {"service": "search", "environment": "staging"},
}
TARGETS = {"staging": {"approval_level": 1}, "production": {"approval_level": 2}}  # deploy-target registry


@dataclass
class FakeCI:
    """Stands in for the CI API: the newest commit of each service and the newest one marked green."""

    newest: dict[str, str] = field(default_factory=lambda: {"checkout": "4e7d0c3", "search": "77b1e20"})
    green: dict[str, str] = field(default_factory=lambda: {"checkout": "9f2c1ab"})  # search: no green build yet

    def green_commit(self, service: str) -> str | None:
        return self.green.get(service)


@dataclass
class FakeDeployer:
    """Stands in for a deploy API that honours idempotency keys (a repeated key starts nothing new)."""

    deployments: dict[str, dict[str, str]] = field(default_factory=dict)  # idempotency key -> deployment
    deploy_calls: int = 0

    def deploy(self, service: str, commit: str, environment: str, idempotency_key: str) -> dict[str, str]:
        self.deploy_calls += 1
        deployment = self.deployments.setdefault(
            idempotency_key,
            {
                "service": service,
                "commit": commit,
                "environment": environment,
                "id": f"dep-{len(self.deployments) + 1}",
            },
        )
        return {"deployment": deployment["id"]}

    def status(self, idempotency_key: str) -> str:
        """Read-only lookup by idempotency key."""
        return "SUCCEEDED" if idempotency_key in self.deployments else "NOT_FOUND"


@dataclass
class OnCall:
    """The approver: answers in order (a person, in a real deployment). Sees only the checked request."""

    answers: list[bool] = field(default_factory=list)
    asked: list[dict[str, Any]] = field(default_factory=list)

    def approve(self, ctx: safety.ActionContext) -> bool:
        self.asked.append(dict(ctx.request))
        return self.answers.pop(0) if self.answers else False  # nobody answered: not approved


def evidence_for(release_id: str, ci: FakeCI) -> dict[str, Any]:
    """From the release request, CI and the deploy-target registry; never from the model."""
    release = RELEASES.get(release_id, {})
    service, environment = release.get("service"), release.get("environment")
    return {
        "service": service,
        "environment": environment,
        "commit": ci.green_commit(service) if service else None,
        "approval_level": TARGETS.get(environment, {}).get("approval_level") if environment else None,
    }


def deploy_tool(
    guard: safety.ActionGuard,
    ci: FakeCI,
    deployer: FakeDeployer,
    oncall: OnCall,
    release_id: str,
    results: list[safety.ActionResult],
) -> Callable[..., dict[str, Any]]:
    """The agent's ``deploy`` tool for one release request. The model chooses the arguments."""

    def deploy(service: str, commit: str, environment: str) -> dict[str, Any]:
        level = TARGETS.get(environment, {}).get("approval_level")  # of the environment the model named
        result = guard.run(
            "deploy",
            {"service": service, "commit": commit, "environment": environment, "approval_level": level},
            evidence_for(release_id, ci),
            lambda ctx: deployer.deploy(
                ctx.request["service"], ctx.request["commit"], ctx.request["environment"], ctx.idempotency_key
            ),
            action_key=f"deploy:{release_id}",  # from the change-management system
            verify=lambda ctx: deployer.status(ctx.idempotency_key),  # read-only: never deploys
            approver=oncall.approve,
        )
        results.append(result)
        if result.final_outcome == "SUCCEEDED":
            return {"status": "deployed"}
        if result.previous_outcome == "SUCCEEDED":
            return {"status": "already_deployed"}
        if result.decision.verdict == "REQUIRE_APPROVAL":
            return {"status": "not_approved"}
        if result.outcome == "NOT_EXECUTED":
            return {"status": "not_allowed", "reasons": list(result.decision.reason_codes)}
        return {"status": "outcome_unknown", "note": "escalated to a person; do not retry"}

    return deploy


class Step(NamedTuple):
    process: str  # "first" or "restarted" (a new guard on the same store file)
    release: str
    note: str
    service: str
    commit: str
    environment: str
    approval: bool | None = None  # the on-call engineer's answer, if asked


STEPS = [
    Step("first", "REL-7", "the agent deploys the newest commit, not yet green", "checkout", "4e7d0c3", "staging"),
    Step("first", "REL-7", "it deploys the green commit to staging", "checkout", "9f2c1ab", "staging"),
    Step("first", "REL-7", "it loops and deploys it again", "checkout", "9f2c1ab", "staging"),
    Step("first", "REL-8", "production; the on-call engineer declines", "checkout", "9f2c1ab", "production", False),
    Step("first", "REL-8", "production again; this time approved", "checkout", "9f2c1ab", "production", True),
    Step("restarted", "REL-8", "after a restart, the same release again", "checkout", "9f2c1ab", "production"),
    Step("restarted", "REL-9", "a second release request, same commit", "checkout", "9f2c1ab", "production", True),
    Step("restarted", "REL-10", "a service with no green build", "search", "77b1e20", "staging"),
]


def explain(result: safety.ActionResult) -> str:
    """One line: what happened, and the reason codes that decided it."""
    d = result.decision
    codes = ", ".join(d.reason_codes)
    if result.final_outcome == "SUCCEEDED":
        approved = ", approved" if result.approval == "granted" else ""
        return f"DEPLOYED {d.verdict} ({codes}{approved})"
    if result.previous_outcome is not None:
        return f"BLOCKED  {codes} [{d.rule}]: earlier attempt {result.previous_outcome}, not repeated"
    if d.verdict == "REQUIRE_APPROVAL":
        return f"BLOCKED  {d.verdict} ({codes}), approval {result.approval}"
    if result.outcome == "NOT_EXECUTED":
        return f"BLOCKED  {d.verdict} {codes} [{d.rule}]"
    return f"{result.final_outcome}  {d.verdict} ({codes}), next: {result.recovery}"


@dataclass
class Run:
    ci: FakeCI = field(default_factory=FakeCI)
    deployer: FakeDeployer = field(default_factory=FakeDeployer)
    oncall: OnCall = field(default_factory=OnCall)
    results: list[safety.ActionResult] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)  # what the model was told


def main() -> Run:
    run = Run()
    policy = safety.load_policy(POLICY)
    # In production: two stable random secrets (32+ bytes each) from a secret manager, the same in
    # every process and after every restart. Generated here because the store is temporary.
    store_secret = secrets.token_bytes(32)
    idempotency = safety.IdempotencyKeys(namespace="release-agent/recipes", secret=secrets.token_bytes(32))
    print("deploy: only the commit CI marked green, once per (service, environment, commit); production approved")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "actions.db"
        guard: safety.ActionGuard | None = None
        process = ""
        for number, step in enumerate(STEPS, 1):
            if step.process != process:  # a new process: only the store file remains
                guard = safety.ActionGuard(
                    policy, store=safety.SqliteActionStore(path, secret=store_secret), idempotency=idempotency
                )
                process = step.process
            assert guard is not None
            run.oncall.answers = [] if step.approval is None else [step.approval]
            tool = deploy_tool(guard, run.ci, run.deployer, run.oncall, step.release, run.results)
            run.replies.append(tool(service=step.service, commit=step.commit, environment=step.environment))
            print(f"{number:>2}  {step.process} process, {step.release}: {step.note}")
            print(f"    deploy(service={step.service!r}, commit={step.commit!r}, environment={step.environment!r})")
            print(f"    -> {explain(run.results[-1])}")
    done = ", ".join(f"{d['service']}@{d['commit']} to {d['environment']}" for d in run.deployer.deployments.values())
    print(
        f"deployer: {len(run.deployer.deployments)} deployment(s) ({done}), {run.deployer.deploy_calls} deploy call(s)"
    )
    print(f"on-call:  asked {len(run.oncall.asked)} time(s)")
    return run


if __name__ == "__main__":
    main()

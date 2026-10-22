"""The action-safety recipes in examples/safety_recipes: each runs offline, and every step is
allowed or blocked for the documented reason, with the side effect happening once."""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections.abc import Iterator
from types import ModuleType

import pytest

from aobs import safety
from sdk_support import REPO_ROOT

RECIPES = REPO_ROOT / "examples" / "safety_recipes"
NAMES = ("send_email", "delete_records", "deploy")


@pytest.fixture
def recipes() -> Iterator[dict[str, ModuleType]]:
    sys.path.insert(0, str(RECIPES))
    try:
        for name in NAMES:
            sys.modules.pop(name, None)
        yield {name: importlib.import_module(name) for name in NAMES}
    finally:
        sys.path.remove(str(RECIPES))
        for name in NAMES:
            sys.modules.pop(name, None)


def steps(results: list[safety.ActionResult]) -> list[tuple[str, tuple[str, ...], str]]:
    return [(r.decision.verdict, r.decision.reason_codes, r.final_outcome) for r in results]


@pytest.mark.parametrize("name", NAMES)
def test_every_recipe_policy_loads(name: str) -> None:
    policy = safety.load_policy(RECIPES / f"{name}_policy.toml")
    assert len(policy.actions) == 1


def test_send_email_sends_each_notification_once_and_only_to_the_customer_on_record(
    recipes: dict[str, ModuleType], capsys: pytest.CaptureFixture[str]
) -> None:
    run = recipes["send_email"].main()
    assert steps(run.results) == [
        ("ALLOW", ("all_checks_passed",), "SUCCEEDED"),
        ("DENY", ("duplicate_action",), "NOT_EXECUTED"),  # the agent loops
        ("DENY", ("evidence_mismatch",), "NOT_EXECUTED"),  # another recipient
        ("DENY", ("evidence_missing",), "NOT_EXECUTED"),  # unknown template
        ("ALLOW", ("all_checks_passed",), "SUCCEEDED"),  # timed out; the status lookup confirmed it
        ("DENY", ("duplicate_action",), "NOT_EXECUTED"),  # the retry after the timeout
        ("DENY", ("duplicate_action",), "NOT_EXECUTED"),  # after a restart
    ]
    timed_out = run.results[4]
    assert timed_out.outcome == "UNKNOWN" and timed_out.verified == "SUCCEEDED" and timed_out.executed
    assert [r.previous_outcome for r in run.results if r.decision.reason_codes == ("duplicate_action",)] == [
        "SUCCEEDED"
    ] * 3
    mail = run.mail
    assert sorted(m["template"] for m in mail.outbox.values()) == ["refund_confirmation", "shipping_update"]
    assert {m["to"] for m in mail.outbox.values()} == {"jane.doe@example.com"}
    assert (mail.send_calls, mail.status_calls) == (2, 1)  # never sent again after the timeout
    assert run.stored_state == "SUCCEEDED"
    assert run.replies[1] == {"status": "already_sent"} and run.replies[6] == {"status": "already_sent"}
    assert run.replies[2] == {"status": "not_sent", "reasons": ["evidence_mismatch"]}
    out = capsys.readouterr().out
    assert "evidence_mismatch [actions.send_email.must_match]" in out and "2 email(s) sent" in out


def test_send_email_denies_a_ticket_without_a_customer_record(recipes: dict[str, ModuleType]) -> None:
    recipe = recipes["send_email"]
    guard = safety.ActionGuard(
        safety.load_policy(RECIPES / "send_email_policy.toml"),
        idempotency=safety.IdempotencyKeys(namespace="tests/recipes", secret=bytes(range(32))),
    )
    mail, results = recipe.FakeMailProvider(), []
    tool = recipe.email_tool(guard, mail, "T-404", results)
    assert tool(to="jane.doe@example.com", template="refund_confirmation")["status"] == "not_sent"
    assert results[0].decision.reason_codes == ("evidence_missing",) and mail.send_calls == 0


def test_delete_records_only_deletes_the_accounts_own_existing_records_within_the_limits(
    recipes: dict[str, ModuleType], capsys: pytest.CaptureFixture[str]
) -> None:
    run = recipes["delete_records"].main()
    assert steps(run.results) == [
        ("ALLOW", ("all_checks_passed",), "SUCCEEDED"),
        ("DENY", ("evidence_missing", "amount_exceeds_evidence"), "NOT_EXECUTED"),  # gone: not deleted twice
        ("DENY", ("evidence_mismatch",), "NOT_EXECUTED"),  # another account's record
        ("DENY", ("amount_exceeds_evidence",), "NOT_EXECUTED"),  # a record that does not exist
        ("REQUIRE_APPROVAL", ("approval_required",), "NOT_EXECUTED"),  # declined
        ("REQUIRE_APPROVAL", ("approval_required",), "SUCCEEDED"),  # approved
        ("DENY", ("amount_exceeds_limit",), "NOT_EXECUTED"),  # above max_amount: no approver asked
    ]
    assert [r.approval for r in run.results[4:6]] == ["denied", "granted"]
    crm = run.crm
    assert len(crm.deleted) == 43 and len(set(crm.deleted)) == 43
    assert all(r.startswith("acme-") for r in crm.deleted)
    assert sum(1 for owner in crm.owners.values() if owner == "globex") == 5  # untouched
    assert crm.delete_calls == 2 and run.officer.asked == [40, 40]
    assert run.replies[5] == {"status": "deleted", "count": 40}
    assert "approval denied" in capsys.readouterr().out


def test_delete_records_reports_a_definite_failure_when_the_records_changed_after_the_check(
    recipes: dict[str, ModuleType],
) -> None:
    recipe = recipes["delete_records"]
    crm = recipe.FakeCRM()
    with pytest.raises(safety.ActionNotPerformed):
        crm.delete("acme", ["acme-001", "globex-001"])
    assert crm.deleted == [] and crm.owner_of("acme-001") == "acme"


def test_deploy_ships_only_the_green_commit_once_per_environment_and_production_needs_approval(
    recipes: dict[str, ModuleType], capsys: pytest.CaptureFixture[str]
) -> None:
    run = recipes["deploy"].main()
    assert steps(run.results) == [
        ("DENY", ("evidence_mismatch",), "NOT_EXECUTED"),  # the newest commit, not the green one
        ("ALLOW", ("all_checks_passed",), "SUCCEEDED"),  # staging
        ("DENY", ("duplicate_action",), "NOT_EXECUTED"),  # the agent loops
        ("REQUIRE_APPROVAL", ("approval_required",), "NOT_EXECUTED"),  # production, declined
        ("REQUIRE_APPROVAL", ("approval_required",), "SUCCEEDED"),  # production, approved
        ("DENY", ("duplicate_action",), "NOT_EXECUTED"),  # restarted: the same release request
        ("DENY", ("duplicate_action",), "NOT_EXECUTED"),  # another release request, same commit
        ("DENY", ("evidence_missing",), "NOT_EXECUTED"),  # no green build
    ]
    assert run.results[5].decision.rule == "guard.action_key"
    assert run.results[6].decision.rule == "actions.deploy.once_per"
    assert run.results[6].previous_outcome == "SUCCEEDED"
    deployments = sorted((d["environment"], d["commit"]) for d in run.deployer.deployments.values())
    assert deployments == [("production", "9f2c1ab"), ("staging", "9f2c1ab")]
    assert run.deployer.deploy_calls == 2
    assert [a["environment"] for a in run.oncall.asked] == ["production", "production"]  # never for duplicates
    assert run.replies[6] == {"status": "already_deployed"}
    assert "approval denied" in capsys.readouterr().out


def test_deploy_cannot_be_talked_into_production_under_a_staging_release(recipes: dict[str, ModuleType]) -> None:
    recipe = recipes["deploy"]
    guard = safety.ActionGuard(
        safety.load_policy(RECIPES / "deploy_policy.toml"),
        idempotency=safety.IdempotencyKeys(namespace="tests/recipes", secret=bytes(range(32))),
    )
    run = recipe.Run()
    run.oncall.answers = [True]
    tool = recipe.deploy_tool(guard, run.ci, run.deployer, run.oncall, "REL-7", run.results)
    assert tool(service="checkout", commit="9f2c1ab", environment="production")["status"] == "not_allowed"
    assert run.results[0].decision.reason_codes == ("evidence_mismatch",)
    assert run.deployer.deploy_calls == 0 and run.oncall.asked == []


@pytest.mark.parametrize("name", NAMES)
def test_every_recipe_runs_as_a_script(name: str) -> None:
    done = subprocess.run(
        [sys.executable, str(RECIPES / f"{name}.py")], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    assert "BLOCKED" in done.stdout and "Traceback" not in done.stderr

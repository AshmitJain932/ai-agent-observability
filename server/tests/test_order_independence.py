"""The assembled result must not depend on arrival order or on retried duplicates (event-model §9)."""

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from aobs_server.model import assemble_events, validate_batch
from support import assembly_config, fixture_files, fixture_id, load_fixture, parse_ts


@pytest.mark.parametrize("path", fixture_files("valid"), ids=fixture_id)
def test_permutations_and_duplicates_do_not_change_result(path: Path) -> None:
    fixture = load_fixture(path)
    expect = fixture["expect"]
    now = parse_ts(expect["assemble_at"])
    config = assembly_config(expect)
    accepted = list(validate_batch(fixture["payload"]).accepted)
    baseline = {tid: v.to_dict() for tid, v in assemble_events(accepted, now, config).items()}

    @settings(
        max_examples=200, deadline=None, database=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(
        order=st.permutations(range(len(accepted))), duplicates=st.lists(st.integers(0, len(accepted) - 1), max_size=6)
    )
    def check(order: list[int], duplicates: list[int]) -> None:
        events = [accepted[i] for i in order] + [accepted[i] for i in duplicates]
        got = {tid: v.to_dict() for tid, v in assemble_events(events, now, config).items()}
        # Duplicates never change the trace views: conflicts are counted per distinct variant.
        assert got == baseline

    check()

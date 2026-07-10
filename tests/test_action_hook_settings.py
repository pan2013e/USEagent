from __future__ import annotations

import pytest

from useagent.action_hook_settings import (
    ActionHookSettings,
    configure_action_hook_settings,
    get_action_hook_settings,
    reset_action_hook_settings,
)
from useagent.main import parse_args


@pytest.fixture(autouse=True)
def reset_settings() -> None:
    reset_action_hook_settings()


def test_action_hook_settings_use_ordered_defaults() -> None:
    settings = ActionHookSettings.resolve(environ={})

    assert settings == ActionHookSettings()
    assert settings.scheduler == "ordered"
    assert settings.max_concurrent_runs == 2
    assert settings.max_unretired_actions == 2


def test_action_hook_settings_resolve_environment_values() -> None:
    settings = ActionHookSettings.resolve(
        environ={
            "USEAGENT_ACTION_HOOK_SCHEDULER": "ordered",
            "USEAGENT_ACTION_HOOK_MAX_CONCURRENT_RUNS": "3",
            "USEAGENT_ACTION_HOOK_MAX_UNRETIRED_ACTIONS": "4",
            "USEAGENT_ACTION_HOOK_RUN_TIMEOUT_SECONDS": "301",
            "USEAGENT_ACTION_HOOK_POST_ACTION_PATIENCE_SECONDS": "1.5",
            "USEAGENT_ACTION_HOOK_INTERVENTION_QUIESCE_SECONDS": "31",
            "USEAGENT_ACTION_HOOK_CLEANUP_SECONDS": "32",
            "USEAGENT_ACTION_HOOK_FINALIZE_SECONDS": "0",
            "USEAGENT_ACTION_HOOK_SNAPSHOT_BUDGET_MIB": "1024.5",
            "USEAGENT_ACTION_HOOK_OBSERVER_QUEUE_CAPACITY": "17",
            "USEAGENT_ACTION_HOOK_OBSERVER_OVERFLOW": "fail",
            "USEAGENT_ACTION_HOOK_WAIT_SECONDS": "0",
        }
    )

    assert settings == ActionHookSettings(
        scheduler="ordered",
        max_concurrent_runs=3,
        max_unretired_actions=4,
        run_timeout_seconds=301.0,
        post_action_patience_seconds=1.5,
        intervention_quiesce_seconds=31.0,
        cleanup_seconds=32.0,
        finalize_seconds=0.0,
        snapshot_budget_mib=1024.5,
        observer_queue_capacity=17,
        observer_overflow="fail",
    )


def test_cli_values_take_precedence_without_parsing_shadowed_environment() -> None:
    settings = ActionHookSettings.resolve(
        scheduler="ordered",
        max_concurrent_runs=5,
        environ={
            "USEAGENT_ACTION_HOOK_SCHEDULER": "invalid",
            "USEAGENT_ACTION_HOOK_MAX_CONCURRENT_RUNS": "invalid",
        },
    )

    assert settings.scheduler == "ordered"
    assert settings.max_concurrent_runs == 5


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        ("USEAGENT_ACTION_HOOK_MAX_CONCURRENT_RUNS", "0"),
        ("USEAGENT_ACTION_HOOK_MAX_UNRETIRED_ACTIONS", "1.5"),
        ("USEAGENT_ACTION_HOOK_RUN_TIMEOUT_SECONDS", "nan"),
        ("USEAGENT_ACTION_HOOK_POST_ACTION_PATIENCE_SECONDS", "-1"),
        ("USEAGENT_ACTION_HOOK_INTERVENTION_QUIESCE_SECONDS", "0"),
        ("USEAGENT_ACTION_HOOK_CLEANUP_SECONDS", "inf"),
        ("USEAGENT_ACTION_HOOK_FINALIZE_SECONDS", "-0.1"),
        ("USEAGENT_ACTION_HOOK_SNAPSHOT_BUDGET_MIB", "0"),
        ("USEAGENT_ACTION_HOOK_OBSERVER_QUEUE_CAPACITY", "0"),
        ("USEAGENT_ACTION_HOOK_OBSERVER_OVERFLOW", "unknown"),
    ],
)
def test_action_hook_settings_reject_invalid_environment_values(
    environment_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        ActionHookSettings.resolve(environ={environment_name: value})


def test_ordered_scheduler_rejects_nonzero_legacy_wait() -> None:
    with pytest.raises(ValueError, match="no longer supported"):
        ActionHookSettings.resolve(
            scheduler="ordered",
            environ={"USEAGENT_ACTION_HOOK_WAIT_SECONDS": "0.1"},
        )


def test_legacy_scheduler_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be one of: ordered"):
        ActionHookSettings.resolve(scheduler="legacy", environ={})


def test_zero_legacy_wait_is_tolerated_for_environment_cleanup() -> None:
    settings = ActionHookSettings.resolve(
        environ={"USEAGENT_ACTION_HOOK_WAIT_SECONDS": "0"}
    )

    assert settings.scheduler == "ordered"


def test_configured_settings_are_available_to_runtime_consumers() -> None:
    configured = configure_action_hook_settings(
        scheduler="ordered",
        max_concurrent_runs=7,
        environ={},
    )

    assert get_action_hook_settings() is configured


def test_useagent_parser_accepts_ordered_scheduler_options() -> None:
    args, destination = parse_args(
        [
            "local",
            "--task-description",
            "noop",
            "--action-hook-scheduler",
            "ordered",
            "--action-hook-max-concurrent-runs",
            "3",
            "--action-hook-max-unretired-actions",
            "4",
            "--action-hook-run-timeout-seconds",
            "301",
            "--action-hook-post-action-patience-seconds",
            "1.5",
            "--action-hook-intervention-quiesce-seconds",
            "31",
            "--action-hook-cleanup-seconds",
            "32",
            "--action-hook-finalize-seconds",
            "0",
            "--action-hook-snapshot-budget-mib",
            "1024.5",
            "--action-hook-observer-queue-capacity",
            "17",
            "--action-hook-observer-overflow",
            "fail",
        ]
    )

    assert destination == "command"
    assert args.action_hook_scheduler == "ordered"
    assert args.action_hook_max_concurrent_runs == 3
    assert args.action_hook_max_unretired_actions == 4
    assert args.action_hook_run_timeout_seconds == 301.0
    assert args.action_hook_post_action_patience_seconds == 1.5
    assert args.action_hook_intervention_quiesce_seconds == 31.0
    assert args.action_hook_cleanup_seconds == 32.0
    assert args.action_hook_finalize_seconds == 0.0
    assert args.action_hook_snapshot_budget_mib == 1024.5
    assert args.action_hook_observer_queue_capacity == 17
    assert args.action_hook_observer_overflow == "fail"


def test_useagent_parser_leaves_scheduler_options_unset() -> None:
    args, _ = parse_args(["local", "--task-description", "noop"])

    assert args.action_hook_scheduler is None
    assert args.action_hook_max_concurrent_runs is None
    assert args.action_hook_max_unretired_actions is None
    assert args.action_hook_run_timeout_seconds is None
    assert args.action_hook_post_action_patience_seconds is None
    assert args.action_hook_intervention_quiesce_seconds is None
    assert args.action_hook_cleanup_seconds is None
    assert args.action_hook_finalize_seconds is None
    assert args.action_hook_snapshot_budget_mib is None
    assert args.action_hook_observer_queue_capacity is None
    assert args.action_hook_observer_overflow is None


def test_useagent_parser_rejects_legacy_scheduler() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "local",
                "--task-description",
                "noop",
                "--action-hook-scheduler",
                "legacy",
            ]
        )

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

ActionHookScheduler = Literal["ordered"]
ObserverOverflow = Literal["drop_oldest", "drop_newest", "fail"]

ACTION_HOOK_SCHEDULERS: tuple[ActionHookScheduler, ...] = ("ordered",)
OBSERVER_OVERFLOW_POLICIES: tuple[ObserverOverflow, ...] = (
    "drop_oldest",
    "drop_newest",
    "fail",
)


@dataclass(frozen=True)
class ActionHookSettings:
    """Validated process-level settings for action-hook scheduling."""

    scheduler: ActionHookScheduler = "ordered"
    max_concurrent_runs: int = 2
    max_unretired_actions: int = 2
    run_timeout_seconds: float = 300.0
    post_action_patience_seconds: float = 0.0
    intervention_quiesce_seconds: float = 30.0
    cleanup_seconds: float = 30.0
    finalize_seconds: float = 60.0
    snapshot_budget_mib: float = 2048.0
    observer_queue_capacity: int = 16
    observer_overflow: ObserverOverflow = "drop_oldest"

    @classmethod
    def resolve(
        cls,
        *,
        scheduler: str | None = None,
        max_concurrent_runs: int | None = None,
        max_unretired_actions: int | None = None,
        run_timeout_seconds: float | None = None,
        post_action_patience_seconds: float | None = None,
        intervention_quiesce_seconds: float | None = None,
        cleanup_seconds: float | None = None,
        finalize_seconds: float | None = None,
        snapshot_budget_mib: float | None = None,
        observer_queue_capacity: int | None = None,
        observer_overflow: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> ActionHookSettings:
        """Resolve CLI overrides before environment values and defaults."""

        values = os.environ if environ is None else environ
        resolved_scheduler = _choice(
            "action hook scheduler",
            _source(
                scheduler,
                values,
                "USEAGENT_ACTION_HOOK_SCHEDULER",
                cls.scheduler,
            ),
            ACTION_HOOK_SCHEDULERS,
        )
        resolved_overflow = _choice(
            "observer overflow policy",
            _source(
                observer_overflow,
                values,
                "USEAGENT_ACTION_HOOK_OBSERVER_OVERFLOW",
                cls.observer_overflow,
            ),
            OBSERVER_OVERFLOW_POLICIES,
        )
        unsupported_wait_seconds = _nonnegative_float(
            "USEAGENT_ACTION_HOOK_WAIT_SECONDS",
            values.get("USEAGENT_ACTION_HOOK_WAIT_SECONDS", 0.0),
        )
        if unsupported_wait_seconds > 0:
            raise ValueError(
                "USEAGENT_ACTION_HOOK_WAIT_SECONDS is no longer supported; use "
                "USEAGENT_ACTION_HOOK_RUN_TIMEOUT_SECONDS and "
                "USEAGENT_ACTION_HOOK_POST_ACTION_PATIENCE_SECONDS instead"
            )

        return cls(
            scheduler=cast(ActionHookScheduler, resolved_scheduler),
            max_concurrent_runs=_positive_int(
                "action-hook max concurrent runs",
                _source(
                    max_concurrent_runs,
                    values,
                    "USEAGENT_ACTION_HOOK_MAX_CONCURRENT_RUNS",
                    cls.max_concurrent_runs,
                ),
            ),
            max_unretired_actions=_positive_int(
                "action-hook max unretired actions",
                _source(
                    max_unretired_actions,
                    values,
                    "USEAGENT_ACTION_HOOK_MAX_UNRETIRED_ACTIONS",
                    cls.max_unretired_actions,
                ),
            ),
            run_timeout_seconds=_positive_float(
                "action-hook run timeout seconds",
                _source(
                    run_timeout_seconds,
                    values,
                    "USEAGENT_ACTION_HOOK_RUN_TIMEOUT_SECONDS",
                    cls.run_timeout_seconds,
                ),
            ),
            post_action_patience_seconds=_nonnegative_float(
                "action-hook post-action patience seconds",
                _source(
                    post_action_patience_seconds,
                    values,
                    "USEAGENT_ACTION_HOOK_POST_ACTION_PATIENCE_SECONDS",
                    cls.post_action_patience_seconds,
                ),
            ),
            intervention_quiesce_seconds=_positive_float(
                "action-hook intervention quiesce seconds",
                _source(
                    intervention_quiesce_seconds,
                    values,
                    "USEAGENT_ACTION_HOOK_INTERVENTION_QUIESCE_SECONDS",
                    cls.intervention_quiesce_seconds,
                ),
            ),
            cleanup_seconds=_positive_float(
                "action-hook cleanup seconds",
                _source(
                    cleanup_seconds,
                    values,
                    "USEAGENT_ACTION_HOOK_CLEANUP_SECONDS",
                    cls.cleanup_seconds,
                ),
            ),
            finalize_seconds=_nonnegative_float(
                "action-hook finalize seconds",
                _source(
                    finalize_seconds,
                    values,
                    "USEAGENT_ACTION_HOOK_FINALIZE_SECONDS",
                    cls.finalize_seconds,
                ),
            ),
            snapshot_budget_mib=_positive_float(
                "action-hook snapshot budget MiB",
                _source(
                    snapshot_budget_mib,
                    values,
                    "USEAGENT_ACTION_HOOK_SNAPSHOT_BUDGET_MIB",
                    cls.snapshot_budget_mib,
                ),
            ),
            observer_queue_capacity=_positive_int(
                "action-hook observer queue capacity",
                _source(
                    observer_queue_capacity,
                    values,
                    "USEAGENT_ACTION_HOOK_OBSERVER_QUEUE_CAPACITY",
                    cls.observer_queue_capacity,
                ),
            ),
            observer_overflow=cast(ObserverOverflow, resolved_overflow),
        )


_configured_settings: ActionHookSettings | None = None


def configure_action_hook_settings(
    *,
    scheduler: str | None = None,
    max_concurrent_runs: int | None = None,
    max_unretired_actions: int | None = None,
    run_timeout_seconds: float | None = None,
    post_action_patience_seconds: float | None = None,
    intervention_quiesce_seconds: float | None = None,
    cleanup_seconds: float | None = None,
    finalize_seconds: float | None = None,
    snapshot_budget_mib: float | None = None,
    observer_queue_capacity: int | None = None,
    observer_overflow: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ActionHookSettings:
    global _configured_settings
    _configured_settings = ActionHookSettings.resolve(
        scheduler=scheduler,
        max_concurrent_runs=max_concurrent_runs,
        max_unretired_actions=max_unretired_actions,
        run_timeout_seconds=run_timeout_seconds,
        post_action_patience_seconds=post_action_patience_seconds,
        intervention_quiesce_seconds=intervention_quiesce_seconds,
        cleanup_seconds=cleanup_seconds,
        finalize_seconds=finalize_seconds,
        snapshot_budget_mib=snapshot_budget_mib,
        observer_queue_capacity=observer_queue_capacity,
        observer_overflow=observer_overflow,
        environ=environ,
    )
    return _configured_settings


def get_action_hook_settings() -> ActionHookSettings:
    if _configured_settings is not None:
        return _configured_settings
    return ActionHookSettings.resolve()


def reset_action_hook_settings() -> None:
    """Clear explicit configuration. Intended for isolated tests."""

    global _configured_settings
    _configured_settings = None


def parse_positive_int(value: str) -> int:
    return _positive_int("value", value)


def parse_positive_float(value: str) -> float:
    return _positive_float("value", value)


def parse_nonnegative_float(value: str) -> float:
    return _nonnegative_float("value", value)


def _source(
    cli_value: object | None,
    environ: Mapping[str, str],
    environment_name: str,
    default: object,
) -> object:
    if cli_value is not None:
        return cli_value
    return environ.get(environment_name, default)


def _choice(name: str, value: object, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be one of: {', '.join(choices)}")
    normalized = value.strip().lower()
    if normalized not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(choices)}")
    return normalized


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be a positive integer")
    if isinstance(value, str) and str(parsed) != value.strip():
        raise ValueError(f"{name} must be a positive integer")
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _positive_float(name: str, value: object) -> float:
    parsed = _finite_float(name, value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_float(name: str, value: object) -> float:
    parsed = _finite_float(name, value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _finite_float(name: str, value: object) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    return parsed

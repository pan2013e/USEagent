import os
import sys
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal

from loguru import logger

from useagent import task_runner
from useagent.action_hook_settings import (
    ACTION_HOOK_SCHEDULERS,
    OBSERVER_OVERFLOW_POLICIES,
    configure_action_hook_settings,
    parse_nonnegative_float,
    parse_positive_float,
    parse_positive_int,
)
from useagent.action_hooks import (
    configure_action_hook_policy_from_environment,
    parse_action_hook_spec_list,
    register_action_hook_specs,
)
from useagent.config import AppConfig, ConfigSingleton
from useagent.flags import USEBENCH_ENABLED
from useagent.pydantic_models.output.action import Action
from useagent.pydantic_models.output.answer import Answer
from useagent.pydantic_models.output.code_change import CodeChange
from useagent.tasks.github_task import GithubTask
from useagent.tasks.local_task import LocalTask
from useagent.tasks.swebench_task import SWEbenchTask
from useagent.tasks.task import Task
from useagent.tasks.usebench_loader import UseBenchTask


def add_common_args(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--output-dir",
        type=str,
        help="Path to the directory that stores the run results.",
    )

    parser.add_argument(
        "--output-type",
        type=parse_output_type,
        default=CodeChange,
        help="Output model type to use. Options: answer, action, codechange.",
    )

    parser.add_argument(
        "--model",
        type=str,
        default="google-gla:gemini-2.0-flash",
        help="Model identifier to use.",
    )

    parser.add_argument(
        "--provider-url",
        type=str,
        default=None,
        help="URL for locally hosted instances like Ollama.",
    )

    parser.add_argument(
        "--task-id",
        type=str,
        help="Unique identifier for the task run.",
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="The log level user for loguru logging to the console.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="If specified, a DEBUG level log will be logger to this location.",
    )
    parser.add_argument(
        "--action-hook",
        action="append",
        default=[],
        help=(
            "Top-level action hook to load at startup, as 'module:function', "
            "'/path/to/file.py:function', or 'command:<shell command>'. Can be "
            "repeated. The environment variable USEAGENT_ACTION_HOOKS also "
            "accepts comma-separated specs."
        ),
    )
    parser.add_argument(
        "--action-hook-disable-restore",
        action="store_true",
        help=(
            "Allow hook interventions but prevent them from restoring action "
            "checkpoints. Equivalent to USEAGENT_ACTION_HOOK_ALLOW_RESTORE=0."
        ),
    )
    parser.add_argument(
        "--action-hook-restore-actions",
        default=None,
        help=(
            "Comma-separated top-level actions whose hook interventions may "
            "restore checkpoints. Defaults to USEAGENT_ACTION_HOOK_RESTORE_ACTIONS "
            "or all actions."
        ),
    )
    parser.add_argument(
        "--action-hook-scheduler",
        choices=ACTION_HOOK_SCHEDULERS,
        default=None,
        help=(
            "Action-hook scheduler mode. Only ordered scheduling is supported; "
            "the option defaults to USEAGENT_ACTION_HOOK_SCHEDULER or ordered."
        ),
    )
    parser.add_argument(
        "--action-hook-max-concurrent-runs",
        type=parse_positive_int,
        default=None,
        metavar="N",
        help="Maximum number of concurrently running action hooks.",
    )
    parser.add_argument(
        "--action-hook-max-unretired-actions",
        type=parse_positive_int,
        default=None,
        metavar="N",
        help="Maximum number of admitted gate actions awaiting retirement.",
    )
    parser.add_argument(
        "--action-hook-run-timeout-seconds",
        type=parse_positive_float,
        default=None,
        metavar="S",
        help="Per-job runtime limit after a hook worker starts the job.",
    )
    parser.add_argument(
        "--action-hook-post-action-patience-seconds",
        type=parse_nonnegative_float,
        default=None,
        metavar="S",
        help="Optional post-action wait before speculative execution may continue.",
    )
    parser.add_argument(
        "--action-hook-intervention-quiesce-seconds",
        type=parse_positive_float,
        default=None,
        metavar="S",
        help="Deadline for active work to reach an intervention-safe boundary.",
    )
    parser.add_argument(
        "--action-hook-cleanup-seconds",
        type=parse_positive_float,
        default=None,
        metavar="S",
        help="Deadline for terminating hook-owned resources.",
    )
    parser.add_argument(
        "--action-hook-finalize-seconds",
        type=parse_nonnegative_float,
        default=None,
        metavar="S",
        help="Deadline for the final mandatory gate-hook drain.",
    )
    parser.add_argument(
        "--action-hook-snapshot-budget-mib",
        type=parse_positive_float,
        default=None,
        metavar="M",
        help="Maximum aggregate action-hook snapshot size in MiB.",
    )
    parser.add_argument(
        "--action-hook-observer-queue-capacity",
        type=parse_positive_int,
        default=None,
        metavar="N",
        help="Default bounded queue capacity for every-event observers.",
    )
    parser.add_argument(
        "--action-hook-observer-overflow",
        choices=OBSERVER_OVERFLOW_POLICIES,
        default=None,
        help="Default observer queue overflow policy.",
    )


def set_usebench_parser_args(parser: ArgumentParser) -> None:
    add_common_args(parser)

    parser.add_argument(
        "--task-list-file",
        type=str,
        help="Path to the file that contains all tasks ids to be run.",
    )


def set_local_parser_args(parser: ArgumentParser) -> None:
    add_common_args(parser)

    parser.add_argument(
        "--project-directory",
        type=Path,
        help="Path to the folder containing the project to operate on.",
    )

    task_group = parser.add_mutually_exclusive_group(required=True)
    task_group.add_argument(
        "--task-description",
        type=str,
        help="Verbatim description of what should be done.",
    )
    task_group.add_argument(
        "--task-file",
        type=Path,
        help="A path to a markdown or text file containing the task.",
    )


def set_github_parser_args(parser: ArgumentParser) -> None:
    add_common_args(parser)
    parser.add_argument(
        "--repo-url",
        type=str,
        required=True,
        help="Git repository to clone (SSH or HTTPS).",
    )
    parser.add_argument(
        "--working-dir",
        type=Path,
        default=Path("/tmp/working_dir"),
        help="Target directory to clone into and work on (within Docker Container).",
    )
    parser.add_argument(
        "--commit",
        type=str,
        required=False,
        help="Commit SHA to checkout and branch from.",
    )

    task_group = parser.add_mutually_exclusive_group(required=True)
    task_group.add_argument(
        "--task-description",
        type=str,
        help="Verbatim description of what should be done.",
    )
    task_group.add_argument(
        "--task-file",
        type=Path,
        help="A path to a markdown or text file containing the task.",
    )


def set_swebench_parser_args(parser: ArgumentParser) -> None:
    add_common_args(parser)
    parser.add_argument(
        "--instance-id",
        type=str,
        required=True,
        help="SWE-bench instance_id to materialize.",
    )
    parser.add_argument(
        "--working-dir",
        type=Path,
        default=SWEbenchTask.get_default_working_dir(),
        help="Target directory to clone into and work on (within Docker Container).",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="princeton-nlp/SWE-bench_Verified",
        help="HF dataset name containing the instance.",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        choices=["train", "validation", "test"],
        help="Dataset split to search for the instance.",
    )


def _get_task_description(args: Namespace) -> str:
    if getattr(args, "task_description", None):
        return args.task_description
    if getattr(args, "task_file", None) and args.task_file.is_file():
        return args.task_file.read_text()
    raise ValueError("Invalid task file")


def parse_args(argv: list[str] | None = None) -> tuple[Namespace, str]:
    parser = ArgumentParser()

    subparser_dest_attr_name = "command"
    subparsers = parser.add_subparsers(dest=subparser_dest_attr_name)

    # TODO: add a common parser for all other kinds of tasks

    usebench_parser = subparsers.add_parser(
        "usebench", help="Run one or multiple usebench tasks."
    )
    set_usebench_parser_args(usebench_parser)

    local_parser = subparsers.add_parser(
        "local", help="Run a task from a description or file."
    )
    set_local_parser_args(local_parser)

    github_parser = subparsers.add_parser(
        "github", help="Run a task on a GitHub repository, from a provided URL."
    )
    set_github_parser_args(github_parser)

    swebench_parser = subparsers.add_parser(
        "swebench", help="Materialize and run a SWE-bench (verified) instance by id."
    )
    set_swebench_parser_args(swebench_parser)

    return parser.parse_args(argv), subparser_dest_attr_name


def handle_command(args: Namespace, subparser_dest_attr_name: str) -> None:
    subcommand = getattr(args, subparser_dest_attr_name, None)
    if subcommand == "usebench":
        if not USEBENCH_ENABLED or UseBenchTask is None:
            raise ValueError(
                "USEBench is not enabled. Set USEBENCH_ENABLED=true and install extras: uv sync --extra usebench"
            )
        uid = args.task_id
        local_path = mkdtemp(prefix=f"acr_usebench_{uid}")
        usebench_task = UseBenchTask(
            uid=uid,
            project_path=local_path,
        )
        task_runner.run(usebench_task, args.output_dir, output_type=CodeChange)

    elif subcommand == "local":
        local_path = args.project_directory
        task_desc = _get_task_description(args)
        local_task = LocalTask(issue_statement=task_desc, project_path=local_path)
        task_runner.run(local_task, args.output_dir, output_type=args.output_type)

    elif subcommand == "github":
        task_desc = _get_task_description(args)
        task = GithubTask(
            issue_statement=task_desc,
            repo_url=args.repo_url,
            working_dir=args.working_dir,
            commit=args.commit,
        )
        task_runner.run(task, args.output_dir, output_type=args.output_type)

    elif subcommand == "swebench":
        task = SWEbenchTask(
            instance_id=args.instance_id,
            working_dir=args.working_dir,
            dataset=args.dataset,
            split=args.split,
        )
        # issue_statement is derived from the dataset; task.uid is instance_id
        task_runner.run(task, args.output_dir, output_type=args.output_type)

    else:
        raise ValueError(f"Unknown command: {subcommand}")


def build_and_register_config(args: Namespace) -> AppConfig:
    output_dir = os.path.abspath(args.output_dir) if args.output_dir else None
    ollama_kwargs = {} if not args.provider_url else {"provider_url": args.provider_url}
    subcommand = args.command
    task_type = _subcommand_to_task_type(subcommand=subcommand)
    output_type = args.output_type
    ConfigSingleton.init(
        model=args.model,
        output_dir=output_dir,
        task_type=task_type,
        output_type=output_type,
        **ollama_kwargs,
    )

    return ConfigSingleton.config


def _subcommand_to_task_type(
    subcommand: str,
) -> type[Task]:
    match subcommand.strip().lower():
        case "github":
            return GithubTask
        case "local":
            return LocalTask
        case "usebench":
            if not USEBENCH_ENABLED or UseBenchTask is None:
                raise ArgumentTypeError(
                    "USEBench is not enabled. Set USEBENCH_ENABLED=true and install extras: uv sync --extra usebench"
                )
            return UseBenchTask
        case "swebench":
            return SWEbenchTask
        case _:
            raise ArgumentTypeError(
                f"Received unsupported subcommand {subcommand} - cannot parse to supported Task Types"
            )


def setup_loguru(console_log_level: str, log_file: str | None) -> None:
    logger.remove()
    fmt = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <6}</level> | <cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
    logger.add(sys.stderr, level=console_log_level.upper(), format=fmt)
    if log_file:
        logger.add(log_file, level="DEBUG", format=fmt)
    logger.info(
        f"Loguru initialized: console={console_log_level.upper()}, file={'enabled @ DEBUG level' if log_file else 'disabled'}"
    )


def parse_output_type(value: str) -> Literal[Answer, CodeChange, Action]:
    if not value:
        raise ArgumentTypeError("Received None for parsing output type")
    match value.strip().lower():
        case "answer":
            return Answer
        case "codechange":
            return CodeChange
        case "action":
            return Action
        case _:
            raise ArgumentTypeError(f"Invalid output type: {value}")


def main():
    args, subparser_dest_attr_name = parse_args()
    setup_loguru(console_log_level=args.log_level, log_file=args.log_file)
    configure_action_hook_settings(
        scheduler=args.action_hook_scheduler,
        max_concurrent_runs=args.action_hook_max_concurrent_runs,
        max_unretired_actions=args.action_hook_max_unretired_actions,
        run_timeout_seconds=args.action_hook_run_timeout_seconds,
        post_action_patience_seconds=args.action_hook_post_action_patience_seconds,
        intervention_quiesce_seconds=args.action_hook_intervention_quiesce_seconds,
        cleanup_seconds=args.action_hook_cleanup_seconds,
        finalize_seconds=args.action_hook_finalize_seconds,
        snapshot_budget_mib=args.action_hook_snapshot_budget_mib,
        observer_queue_capacity=args.action_hook_observer_queue_capacity,
        observer_overflow=args.action_hook_observer_overflow,
    )
    build_and_register_config(args)
    configure_action_hook_policy_from_environment(
        allow_restore=False if args.action_hook_disable_restore else None,
        restore_actions_value=args.action_hook_restore_actions,
    )
    hook_specs = parse_action_hook_spec_list(os.environ.get("USEAGENT_ACTION_HOOKS"))
    hook_specs.extend(args.action_hook)
    if hook_specs:
        registered_hooks = register_action_hook_specs(hook_specs)
        logger.info(f"Registered {registered_hooks} top-level action hook(s)")
    handle_command(args, subparser_dest_attr_name)


if __name__ == "__main__":
    main()

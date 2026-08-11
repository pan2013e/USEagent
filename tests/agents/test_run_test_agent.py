import pytest
from pydantic_ai import models
from pydantic_ai.models.test import TestModel

from useagent.agents.test_execution.agent import (
    SYSTEM_PROMPT,
    format_environment_command_information,
    init_agent,
)
from useagent.config import AppConfig
from useagent.pydantic_models.info.environment import Commands
from useagent.tools.meta import execute_tests

models.ALLOW_MODEL_REQUESTS = False


@pytest.mark.agent
def test_init_test_execution_agent_can_be_initialized():
    test_model = TestModel()
    config = AppConfig(model=test_model)

    agent = init_agent(config=config)

    assert len(agent._instructions) >= 1
    # DevNote: Output style instructions are only added at Runtime, not at build time.
    # For the initial instructions there is nothing, but we also don't want to see failures.


def test_test_instructions_prioritize_reduced_native_tests() -> None:
    commands = Commands(
        test_command="python setup.py test",
        reducable_test_scope=True,
        example_reduced_test_command="bin/test package/tests/test_feature.py",
    )

    information = format_environment_command_information(commands)

    assert information.index("Reduced test example") < information.index(
        "General project test command"
    )
    assert "Begin with the smallest relevant test scope" in SYSTEM_PROMPT
    assert "Do not repeat an unchanged successful full-suite command" in SYSTEM_PROMPT
    assert "focused native test command first" in (execute_tests.__doc__ or "")

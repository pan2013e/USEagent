import os
import subprocess

import pytest

from useagent.agents.meta.agent import _resolve_code_change_diff
from useagent.pydantic_models.output.code_change import CodeChange
from useagent.pydantic_models.task_state import TaskState
from useagent.state.git_repo import GitRepository
from useagent.tasks.test_task import TestTask


@pytest.fixture
def changed_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    target = tmp_path / "target.py"
    target.write_text("before\n")
    subprocess.run(["git", "add", "target.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True)
    target.write_text("after\n")
    return tmp_path


@pytest.mark.asyncio
async def test_resolve_code_change_diff_recovers_missing_key(changed_repo):
    previous_cwd = os.getcwd()
    os.chdir(changed_repo)
    try:
        state = TaskState(
            task=TestTask(root=".", issue_statement="Update target"),
            git_repo=GitRepository(local_path=changed_repo),
        )
        code_change = CodeChange(
            explanation="Updated target", diff_id="diff_7", doubts=None
        )

        entry = await _resolve_code_change_diff(state, code_change)

        assert code_change.diff_id == "diff_0"
        assert "-before" in entry.diff_content
        assert "+after" in entry.diff_content
    finally:
        os.chdir(previous_cwd)


@pytest.mark.asyncio
async def test_resolve_code_change_diff_fails_when_no_patch_exists(changed_repo):
    subprocess.run(["git", "add", "target.py"], cwd=changed_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "apply update"], cwd=changed_repo, check=True
    )
    previous_cwd = os.getcwd()
    os.chdir(changed_repo)
    try:
        state = TaskState(
            task=TestTask(root=".", issue_statement="Update target"),
            git_repo=GitRepository(local_path=changed_repo),
        )
        code_change = CodeChange(
            explanation="Updated target", diff_id="diff_7", doubts=None
        )

        with pytest.raises(RuntimeError, match="could not be captured"):
            await _resolve_code_change_diff(state, code_change)
    finally:
        os.chdir(previous_cwd)

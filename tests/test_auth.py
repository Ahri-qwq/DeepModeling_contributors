"""Tests for token acquisition."""
import os
import subprocess
import pytest
from contributors.auth import get_token, AuthError


def test_github_token_env_var_returned_directly(monkeypatch):
    """GITHUB_TOKEN environment variable is returned directly."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "token_from_github_token")
    assert get_token() == "token_from_github_token"


def test_gh_token_env_var_returned_when_github_token_missing(monkeypatch):
    """GH_TOKEN is used when GITHUB_TOKEN is not set."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "token_from_gh_token")
    assert get_token() == "token_from_gh_token"


def test_github_token_prioritized_over_gh_token(monkeypatch):
    """GITHUB_TOKEN takes priority when both are set."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "github_token")
    monkeypatch.setenv("GH_TOKEN", "gh_token")
    assert get_token() == "github_token"


def test_empty_env_var_treated_as_unset(monkeypatch):
    """Empty or whitespace-only env vars are treated as unset."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    monkeypatch.setenv("GH_TOKEN", "   ")
    # Should attempt to use gh, which we'll mock to fail
    import shutil
    monkeypatch.setattr(shutil, "which", lambda x: None)
    with pytest.raises(AuthError, match="未找到 token"):
        get_token()


def test_calls_gh_auth_token_when_env_vars_absent(monkeypatch):
    """Calls 'gh auth token' when environment variables are not set."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/gh")

    import contributors.auth as auth_module
    mock_result = subprocess.CompletedProcess(
        args=["gh", "auth", "token"],
        returncode=0,
        stdout="token_from_gh_auth\n",
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_result)
    assert get_token() == "token_from_gh_auth"


def test_raises_when_gh_not_found(monkeypatch):
    """Raises AuthError when 'gh' binary is not found."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda x: None)
    with pytest.raises(AuthError, match="未找到 token"):
        get_token()


def test_raises_when_gh_auth_token_fails(monkeypatch):
    """Raises AuthError when 'gh auth token' command fails."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/gh")

    def mock_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "gh auth token")

    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(AuthError, match="执行失败"):
        get_token()


def test_raises_when_gh_auth_token_timeout(monkeypatch):
    """Raises AuthError when 'gh auth token' times out."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/gh")

    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh auth token", 30)

    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(AuthError, match="执行失败"):
        get_token()


def test_raises_when_gh_auth_token_returns_empty(monkeypatch):
    """Raises AuthError when 'gh auth token' returns empty output."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/gh")

    mock_result = subprocess.CompletedProcess(
        args=["gh", "auth", "token"],
        returncode=0,
        stdout="   \n",
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_result)
    with pytest.raises(AuthError, match="返回空值"):
        get_token()

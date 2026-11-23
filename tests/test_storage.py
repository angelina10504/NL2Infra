"""git_commit(): real git first; NL2INFRA_GIT_COMMIT only when git cannot answer (container image)."""
import subprocess

import storage


def test_git_commit_prefers_git_over_the_env_var(monkeypatch):
    monkeypatch.setenv("NL2INFRA_GIT_COMMIT", "from-env")
    monkeypatch.setattr(storage.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="abc123\n", stderr=""))
    assert storage.git_commit() == "abc123"


def test_git_commit_falls_back_to_env_var_when_git_is_missing(monkeypatch):
    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(storage.subprocess, "run", no_git)
    monkeypatch.setenv("NL2INFRA_GIT_COMMIT", "from-env")
    assert storage.git_commit() == "from-env"


def test_git_commit_falls_back_when_not_a_repository(monkeypatch):
    monkeypatch.setattr(storage.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 128, stdout="", stderr="not a git repository"))
    monkeypatch.setenv("NL2INFRA_GIT_COMMIT", "from-env")
    assert storage.git_commit() == "from-env"


def test_git_commit_is_none_without_git_or_env_var(monkeypatch):
    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(storage.subprocess, "run", no_git)
    monkeypatch.delenv("NL2INFRA_GIT_COMMIT", raising=False)
    assert storage.git_commit() is None

from pathlib import Path

from lockstep.config import Config
from lockstep.gitutil import git


def test_lake_without_login_path_uses_executable_elan_shim(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "missing"))
    shim = tmp_path / ".elan/bin/lake"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n")
    cfg = Config(tmp_path)
    assert cfg.lake == "lake"  # A nonexecutable file must not be selected.
    shim.chmod(0o755)
    assert cfg.lake == str(shim)
    cfg.raw = {"build": {"lake": "/custom/toolchain/lake"}}
    assert cfg.lake == "/custom/toolchain/lake"


def test_path_lake_takes_precedence_over_elan(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    lake = tmp_path / "lake"
    lake.write_text("#!/bin/sh\nexit 0\n")
    lake.chmod(0o755)
    assert Config(tmp_path).lake == "lake"


def test_harness_git_disables_automatic_background_maintenance(cfg):
    # Ask Git for its effective config, including command-line configuration.
    assert git(cfg, "config", "--get", "gc.auto").strip() == "0"
    assert git(cfg, "config", "--get", "maintenance.auto").strip() == "false"

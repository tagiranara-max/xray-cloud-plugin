from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("install_skills", ROOT / "scripts" / "install_skills.py")
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)

NAMES = ["import-results", "import-tests", "predict-affected-tests", "xray-setup"]


def test_install_copies_all_four_skills(tmp_path):
    plan = inst.install("codex", "project", project=tmp_path)
    assert [n for n, _ in plan] == NAMES
    for n in NAMES:
        assert (tmp_path / ".agents" / "skills" / n / "SKILL.md").is_file()
        assert not (tmp_path / ".agents" / "skills" / n).is_symlink()


@pytest.mark.parametrize("client,rel", [("cursor", ".cursor/skills"), ("gemini", ".gemini/skills")])
def test_user_scope_paths(tmp_path, client, rel):
    inst.install(client, "user", home=tmp_path)
    assert (tmp_path / rel / "xray-setup" / "SKILL.md").is_file()


def test_existing_different_skill_refused_nothing_written(tmp_path):
    dest = tmp_path / ".cursor" / "skills"
    (dest / "xray-setup").mkdir(parents=True)
    (dest / "xray-setup" / "SKILL.md").write_text("mine", encoding="utf-8")
    (dest / "other").mkdir()
    with pytest.raises(FileExistsError, match="xray-setup"):
        inst.install("cursor", "project", project=tmp_path)
    assert (dest / "xray-setup" / "SKILL.md").read_text(encoding="utf-8") == "mine"
    assert sorted(p.name for p in dest.iterdir()) == ["other", "xray-setup"]


def test_identical_skill_skipped(tmp_path):
    inst.install("gemini", "project", project=tmp_path)
    plan = inst.install("gemini", "project", project=tmp_path)
    assert {a for _, a in plan} == {"skipped"}


def test_force_overwrites_only_our_skills(tmp_path):
    dest = tmp_path / ".agents" / "skills"
    (dest / "xray-setup").mkdir(parents=True)
    (dest / "xray-setup" / "SKILL.md").write_text("mine", encoding="utf-8")
    (dest / "xray-setup" / "stale.txt").write_text("x", encoding="utf-8")
    (dest / "other").mkdir()
    (dest / "other" / "SKILL.md").write_text("keep", encoding="utf-8")
    plan = dict(inst.install("codex", "project", project=tmp_path, force=True))
    assert plan["xray-setup"] == "overwritten"
    assert "name: xray-setup" in (dest / "xray-setup" / "SKILL.md").read_text(encoding="utf-8")
    assert not (dest / "xray-setup" / "stale.txt").exists()
    assert (dest / "other" / "SKILL.md").read_text(encoding="utf-8") == "keep"


def test_dry_run_writes_nothing(tmp_path, capsys):
    assert inst.main(["--client", "codex", "--scope", "project", "--project", str(tmp_path), "--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert not (tmp_path / ".agents").exists()


def test_unknown_client_or_scope_errors(tmp_path):
    with pytest.raises(ValueError):
        inst.install("vim", "project", project=tmp_path)
    with pytest.raises(ValueError):
        inst.install("codex", "galaxy", project=tmp_path)
    with pytest.raises(SystemExit):
        inst.main(["--client", "vim", "--scope", "user"])


def test_cli_conflict_exit_code(tmp_path, capsys):
    (tmp_path / ".agents" / "skills" / "xray-setup").mkdir(parents=True)
    rc = inst.main(["--client", "codex", "--scope", "project", "--project", str(tmp_path)])
    assert rc == 1
    assert "xray-setup" in capsys.readouterr().err

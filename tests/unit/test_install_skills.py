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


def test_failed_copy_keeps_old_skill_and_reports(tmp_path, monkeypatch, capsys):
    dest = tmp_path / ".agents" / "skills"
    (dest / "import-results").mkdir(parents=True)
    (dest / "import-results" / "SKILL.md").write_text("old", encoding="utf-8")
    (dest / "import-tests").mkdir()
    (dest / "import-tests" / "SKILL.md").write_text("old2", encoding="utf-8")
    real = inst.shutil.copytree
    calls = []

    def boom(src, dst, *a, **k):
        calls.append(dst)
        if len(calls) == 2:
            Path(dst).mkdir(parents=True)
            raise OSError("disk full")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(inst.shutil, "copytree", boom)
    rc = inst.main(["--client", "codex", "--scope", "project", "--project", str(tmp_path), "--force"])
    assert rc == 1
    cap = capsys.readouterr()
    assert "import-tests" in cap.err and "disk full" in cap.err
    assert "overwritten import-results" in cap.out
    assert (dest / "import-tests" / "SKILL.md").read_text(encoding="utf-8") == "old2"
    assert not [p for p in dest.iterdir() if ".tmp-" in p.name]


def test_target_dir_is_a_file_refused(tmp_path, capsys):
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "skills").write_text("x", encoding="utf-8")
    for extra in ([], ["--dry-run"]):
        rc = inst.main(["--client", "codex", "--scope", "project", "--project", str(tmp_path)] + extra)
        assert rc == 1
        assert "not a directory" in capsys.readouterr().err


def test_symlink_target_replaced_link_target_untouched(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k", encoding="utf-8")
    dest = tmp_path / "p" / ".agents" / "skills"
    dest.mkdir(parents=True)
    try:
        (dest / "xray-setup").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(FileExistsError):
        inst.install("codex", "project", project=tmp_path / "p")
    inst.install("codex", "project", project=tmp_path / "p", force=True)
    assert not (dest / "xray-setup").is_symlink()
    assert (dest / "xray-setup" / "SKILL.md").is_file()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "k"


def test_file_in_place_of_skill_needs_force(tmp_path):
    dest = tmp_path / ".gemini" / "skills"
    dest.mkdir(parents=True)
    (dest / "xray-setup").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        inst.install("gemini", "project", project=tmp_path)
    inst.install("gemini", "project", project=tmp_path, force=True)
    assert (dest / "xray-setup" / "SKILL.md").is_file()


def test_extra_file_or_hidden_file_counts_as_different(tmp_path):
    inst.install("codex", "project", project=tmp_path)
    (tmp_path / ".agents" / "skills" / "xray-setup" / ".DS_Store").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        inst.install("codex", "project", project=tmp_path)

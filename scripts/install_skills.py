#!/usr/bin/env python3
"""Copy the plugin skills into the skills folder of Codex, Cursor or Gemini CLI.

Claude Code users install the plugin instead. Copies (never symlinks) so it
works on Windows. Only folders named like our own skills are ever touched.
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

SKILLS_SRC = Path(__file__).resolve().parents[1] / "plugin" / "skills"

# Folder (relative to the project dir / the home dir) per client and scope.
# Confirmed against the official docs on 2026-11-24:
#   codex:  https://developers.openai.com/codex/skills (redirects to
#           https://learn.chatgpt.com/docs/build-skills): .agents/skills, ~/.agents/skills
#   cursor: https://cursor.com/docs/skills: .cursor/skills (also .agents/skills),
#           ~/.cursor/skills (also ~/.agents/skills)
#   gemini: https://geminicli.com/docs/cli/skills/: .gemini/skills (alias
#           .agents/skills), ~/.gemini/skills (alias ~/.agents/skills)
TARGETS = {
    "codex": {"project": ".agents/skills", "user": ".agents/skills"},
    "cursor": {"project": ".cursor/skills", "user": ".cursor/skills"},
    "gemini": {"project": ".gemini/skills", "user": ".gemini/skills"},
}


def _same(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(str(a), str(b))
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(str(a), str(b), cmp.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(_same(a / d, b / d) for d in cmp.common_dirs)


def target_dir(client: str, scope: str, project: Path = None, home: Path = None) -> Path:
    if client not in TARGETS:
        raise ValueError("unknown client %r (choose from %s)" % (client, ", ".join(sorted(TARGETS))))
    if scope not in ("project", "user"):
        raise ValueError("unknown scope %r (choose project or user)" % scope)
    base = (Path(project) if project else Path.cwd()) if scope == "project" else (home or Path.home())
    return Path(base) / TARGETS[client][scope]


def install(client: str, scope: str, project: Path = None, force: bool = False,
            dry_run: bool = False, home: Path = None, src: Path = SKILLS_SRC) -> list:
    """Return a list of (name, action) with action copied|skipped|overwritten."""
    dest = target_dir(client, scope, project, home)
    names = sorted(p.name for p in Path(src).iterdir() if (p / "SKILL.md").is_file())
    plan, conflicts = [], []
    for name in names:  # all checks happen before the first write
        target = dest / name
        if not target.exists():
            plan.append((name, "copied"))
        elif target.is_dir() and _same(Path(src) / name, target):
            plan.append((name, "skipped"))
        elif force:
            plan.append((name, "overwritten"))
        else:
            conflicts.append(name)
    if conflicts:
        raise FileExistsError(
            "refusing to overwrite differing skill(s) in %s: %s (use --force)" % (dest, ", ".join(conflicts)))
    if not dry_run:
        for name, action in plan:
            if action == "skipped":
                continue
            target = dest / name
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(str(target))
            elif target.exists() or target.is_symlink():
                target.unlink()
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(Path(src) / name), str(target))
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client", required=True, choices=sorted(TARGETS))
    ap.add_argument("--scope", required=True, choices=["project", "user"])
    ap.add_argument("--project", help="project directory (default: current directory)")
    ap.add_argument("--force", action="store_true", help="overwrite differing skills of the same name")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen, write nothing")
    args = ap.parse_args(argv)
    try:
        plan = install(args.client, args.scope, Path(args.project) if args.project else None,
                       args.force, args.dry_run)
    except (FileExistsError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    dest = target_dir(args.client, args.scope, Path(args.project) if args.project else None)
    prefix = "[dry-run] " if args.dry_run else ""
    for name, action in plan:
        print("%s%s %s -> %s" % (prefix, action, name, dest / name))
    return 0


if __name__ == "__main__":
    sys.exit(main())

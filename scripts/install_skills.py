#!/usr/bin/env python3
"""Copy the plugin skills into the skills folder of Codex, Cursor or Gemini CLI.

Claude Code users install the plugin instead. Copies (never symlinks) so it
works on Windows. Only folders named like our own skills are ever touched.
"""
from __future__ import annotations

import argparse
import filecmp
import os
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


def _entries(root: Path) -> dict:
    """Map relative path -> kind for everything under root, without following links."""
    out = {}
    for cur, dirs, files in os.walk(str(root), followlinks=False):
        for n in dirs + files:
            full = os.path.join(cur, n)
            rel = os.path.relpath(full, str(root))
            out[rel] = "link" if os.path.islink(full) else ("dir" if n in dirs else "file")
    return out


def _same(a: Path, b: Path) -> bool:
    if os.path.islink(str(b)) or not os.path.isdir(str(b)):
        return False
    ea, eb = _entries(a), _entries(b)
    if ea != eb:
        return False
    for rel, kind in ea.items():
        pa, pb = os.path.join(str(a), rel), os.path.join(str(b), rel)
        if kind == "link" and os.readlink(pa) != os.readlink(pb):
            return False
        if kind == "file" and not filecmp.cmp(pa, pb, shallow=False):
            return False
    return True


class InstallError(Exception):
    def __init__(self, message: str, done: list):
        super().__init__(message)
        self.done = done


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
    for part in [dest] + list(dest.parents):
        if part.exists() and not part.is_dir():
            raise ValueError("%s exists and is not a directory" % part)
        if part.exists():
            break
    plan, conflicts = [], []
    for name in names:  # all checks happen before the first write
        target = dest / name
        if not (target.exists() or target.is_symlink()):
            plan.append((name, "copied"))
        elif _same(Path(src) / name, target):
            plan.append((name, "skipped"))
        elif force:
            plan.append((name, "overwritten"))
        else:
            conflicts.append(name)
    if conflicts:
        raise FileExistsError(
            "refusing to overwrite differing skill(s) in %s: %s (use --force)" % (dest, ", ".join(conflicts)))
    if dry_run:
        return plan
    done = []
    for name, action in plan:
        if action == "skipped":
            done.append((name, action))
            continue
        target = dest / name
        tmp = dest / (".%s.tmp-%d" % (name, os.getpid()))
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(Path(src) / name), str(tmp))
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(str(target))
            os.rename(str(tmp), str(target))
        except OSError as exc:
            shutil.rmtree(str(tmp), ignore_errors=True)
            raise InstallError("failed to install %s into %s: %s" % (name, dest, exc), done)
        done.append((name, action))
    return done


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
    except InstallError as exc:
        for name, action in exc.done:
            print("%s %s" % (action, name))
        print("error: %s" % exc, file=sys.stderr)
        return 1
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

"""Repository guards: version policy and no company-specific strings."""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PLUGIN_JSON = REPO / "plugin" / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO / ".claude-plugin" / "marketplace.json"

# sha256 of lower-cased forbidden tokens (hashes only, never the words).
FORBIDDEN_SHA256 = {
    "6baa9a394a617fa5b5ee54597c5dd1c6c925633f0472582c6a99b96edf60ee3a",
    "2c4dcd884aae621e654b22326136b4de47147ac0476e77728d08adf6bd663526",
    "b62f282368d65b865ab69b4f8f855809a294b602d73eaabf788262cb26d6a351",
    "7a9d8c4ad3c66b7a1399aaa4be2b8886ec956685507898cf96be159ef44ae629",
    "047f46874b7b64b86e17eb3b679657f80b2c4e659acdcfdf1f81ffdb092463c0",
    "21464651041b2f313089dda6c58a81d7cff5a400419469a8fba2ce5c94dea74b",
    "bb0c5a9fd6a46579937bdee8bf64471a861201bfd37e2674ba47d63eee4c1eea",
}

# Hex-encoded substrings that must not appear anywhere in tracked text, in any case
# (stored encoded so this file does not contain them either).
FORBIDDEN_SUBSTRINGS_HEX = ("706179", "63617368")

EXCLUDE = [".git", "tests/fixtures", ".pytest_cache", "__pycache__"]


def git_ignored_dirs():
    """Local directories git ignores (e.g. via .git/info/exclude): not repo content."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
            cwd=REPO, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.rstrip("/") for line in out.splitlines() if line.endswith("/")]


TOKEN_RE = re.compile(r"[A-Za-z0-9-]+")


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iter_word_tokens(root, exclude=()):
    """Yield (relative path, token) for every token in every text file.

    Tokens are runs of [A-Za-z0-9-]; each is yielded whole and also split on '-'
    so that a UUID is one token and 'abc-123' also yields 'abc' and '123'.
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = [
            d for d in dirnames
            if not any(
                (f"{rel_dir}/{d}" if rel_dir != "." else d) == ex or d == ex
                for ex in exclude
            )
        ]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = path.relative_to(root).as_posix()
            for tok in TOKEN_RE.findall(text):
                yield rel, tok
                if "-" in tok:
                    for sub in tok.split("-"):
                        if sub:
                            yield rel, sub


def test_no_plugin_version():
    """No "version" in plugin.json or the marketplace entry: Claude Code then versions the
    plugin by git commit, so every update reaches users. Only gemini-extension.json carries
    a "version" (a placeholder, 0.0.0), because the Gemini manifest format expects one."""
    assert "version" not in load(PLUGIN_JSON)
    assert all("version" not in p for p in load(MARKETPLACE_JSON)["plugins"])


def test_no_company_strings():
    hits = [
        (path, "<forbidden>")
        for path, tok in iter_word_tokens(REPO, exclude=EXCLUDE + git_ignored_dirs())
        if sha256(tok.lower()) in FORBIDDEN_SHA256
    ]
    assert hits == []


def test_no_forbidden_substrings():
    needles = [bytes.fromhex(h).decode() for h in FORBIDDEN_SUBSTRINGS_HEX]
    hits = set()
    for path, tok in iter_word_tokens(REPO, exclude=EXCLUDE + git_ignored_dirs()):
        low = tok.lower()
        if any(n in low for n in needles):
            hits.add(path)
    assert sorted(hits) == []


def test_tokenizer_catches_subtokens(tmp_path):
    (tmp_path / "a.txt").write_text("see abc-123 and 11111111-2222-3333-4444-555555555555")
    toks = {t for _, t in iter_word_tokens(tmp_path)}
    assert {"abc-123", "abc", "123", "11111111-2222-3333-4444-555555555555"} <= toks

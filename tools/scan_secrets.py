#!/usr/bin/env python3
"""One secret scanner, four scopes — and it is meant to run BEFORE a commit.

This exists because of a real incident. On 2026-09-22 a `.env.user-endpoint.bak`
holding three live credentials was committed and pushed to a public repository.
Two separate guards failed to stop it:

* `.gitignore` listed `.env` as an exact name, and the backup had a suffix;
* the repo's secret check walked a LIST OF EXTENSIONS — `.py/.md/.txt/.yml/
  .toml` — and `.bak` was not on it, so it printed "39 files scanned, nothing
  credential-shaped" in the very commit that carried the keys.

Both were after-the-fact checks in CI. By the time CI could have complained,
the objects were already on a public server — and deleting the branch does NOT
remove them: a ref is a pointer, and GitHub serves unreachable objects by SHA.
So the only guard that actually protects anything is one that runs on YOUR
machine, before the commit object exists.

    python3 tools/scan_secrets.py --staged     # what is about to be committed
    python3 tools/scan_secrets.py --worktree   # every tracked file
    python3 tools/scan_secrets.py --range A..B # commits about to be pushed
    python3 tools/scan_secrets.py --history    # every blob that ever existed

Exit 0 clean, 1 if anything matched. `--history` is the one to run before
making a repository public.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

# A credentials-shaped URL is everywhere in this repo's documentation, as
# `http://user:pass@host:port` templates and as `***:***@` in masked log
# output. A match only counts when NEITHER half of the userinfo looks like a
# placeholder — otherwise the check is noise, and a check that is always red
# teaches everyone to ignore it.
PLACEHOLDER = re.compile(
    r"^(\*+|user|username|login|pass|password|passwd|secret|token|key|"
    r"changeme|example[\w-]*|your[_\w-]*|xxx+|\.\.\.|<[^>]*>|\{[^}]*\}|"
    r"[A-Z][A-Z0-9_-]*)$", re.I)

CREDENTIALLED_URL = re.compile(r"[a-z][a-z0-9+.-]*://([^/@:\s\"'<>]+):([^/@\s\"'<>]+)@")
# 32 hex is the shape of a 2captcha key. An image CDN names its files by
# content hash, so a hex that is part of an asset URL does not count.
HEX32 = re.compile(r"(?<![/\w])[0-9a-f]{32}(?!\.(?:avif|jpe?g|png|webp|gif|svg))\b")
# The vendor's own login shapes, which are unmistakable and have no
# placeholder form.
VENDOR = (
    (re.compile(r"-zone-scraping_browser-"), "a Scraping Browser login"),
    (re.compile(r"-zone-custom-region-"), "a proxy gateway login"),
    (re.compile(r"\bclientKey\s*[:=]\s*[\"'][0-9a-f]{20,}"), "a hardcoded API key"),
)
# Any .env that is not the documented example is a finding on its own, whatever
# is inside it. This is the rule the incident actually needed: the file was
# caught by no content pattern because nobody scanned it at all.
ENV_FILE = re.compile(r"(^|/)\.env($|\.)(?!example$)")
RISKY_NAME = re.compile(r"\.(bak|orig|save|backup|swp|key|pem|p12|pfx)$|"
                        r"(^|/)(secrets?|credentials?)[^/]*$", re.I)

TEMPLATE_MARKER = re.compile(r"[{<]\w+[}>]|example|placeholder|your[_-]", re.I)

SKIP = ("smoke_test.py", "tools/scan_secrets.py", ".gitignore",
        ".githooks/pre-commit", ".githooks/pre-push")


def real_credential(match) -> bool:
    user, password = match.group(1), match.group(2)
    if PLACEHOLDER.match(user) or PLACEHOLDER.match(password):
        return False
    return len(user) >= 8 and len(password) >= 8


def scan_text(path: str, text: str):
    """Yield '<what>' strings for everything credential-shaped in `text`."""
    out = []
    if ENV_FILE.search(path):
        out.append("a .env file (only .env.example may be committed)")
    if RISKY_NAME.search(path):
        out.append("a backup/key-shaped filename")
    if path in SKIP:
        # These files carry the patterns they hunt for.
        return out
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in CREDENTIALLED_URL.finditer(line):
            if real_credential(match):
                out.append("line %d: a URL carrying a real password" % lineno)
                break
        for hit in HEX32.findall(line):
            if "example" in line.lower():
                continue
            out.append("line %d: %s… — a 32-hex key shape" % (lineno, hit[:6]))
            break
        # The vendor login shapes appear in this repo's own DOCUMENTATION as
        # templates — `ws://{login}-zone-scraping_browser-...:{password}@...`
        # in .env.example and env_config.py. A template is not a credential,
        # and flagging one makes the check noise. Require the line to carry no
        # placeholder markers.
        if not TEMPLATE_MARKER.search(line):
            for pattern, what in VENDOR:
                if pattern.search(line):
                    out.append("line %d: %s" % (lineno, what))
                    break
    return out


def _git(*args) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def _report(findings) -> int:
    if not findings:
        return 0
    print("\n*** SECRET SCAN FAILED — %d finding(s) ***\n" % len(findings),
          file=sys.stderr)
    for where, what in findings:
        print("  %s\n      %s" % (where, what), file=sys.stderr)
    print("\nNothing has been committed or pushed.\n"
          "Remember: deleting a branch afterwards does NOT remove the objects.\n"
          "A pushed secret is a disclosed secret — rotate it, do not hide it.\n",
          file=sys.stderr)
    return 1


def scan_staged() -> int:
    names = [n for n in _git("diff", "--cached", "--name-only", "-z").split("\0") if n]
    findings = []
    for name in names:
        blob = _git("show", ":%s" % name)
        for what in scan_text(name, blob):
            findings.append((name, what))
    return _report(findings)


def scan_worktree() -> int:
    names = [n for n in _git("ls-files", "-z").split("\0") if n]
    findings = []
    for name in names:
        try:
            with open(name, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for what in scan_text(name, text):
            findings.append((name, what))
    return _report(findings)


def _scan_commits(revs) -> int:
    findings = []
    seen = set()
    for rev in revs:
        for line in _git("ls-tree", "-r", "--full-tree", rev).splitlines():
            try:
                meta, name = line.split("\t", 1)
                blob = meta.split()[2]
            except (ValueError, IndexError):
                continue
            if (blob, name) in seen:
                continue
            seen.add((blob, name))
            for what in scan_text(name, _git("cat-file", "-p", blob)):
                findings.append(("%s @ %s" % (name, rev[:9]), what))
    return _report(findings)


def scan_range(rev_range: str) -> int:
    revs = [r for r in _git("rev-list", rev_range).split() if r]
    return _scan_commits(revs)


def scan_history() -> int:
    revs = [r for r in _git("rev-list", "--all").split() if r]
    print("scanning %d commit(s) — every blob that ever existed" % len(revs))
    return _scan_commits(revs)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true")
    group.add_argument("--worktree", action="store_true")
    group.add_argument("--range", dest="rev_range")
    group.add_argument("--history", action="store_true")
    args = parser.parse_args(argv)

    if args.staged:
        code = scan_staged()
    elif args.worktree:
        code = scan_worktree()
    elif args.rev_range:
        code = scan_range(args.rev_range)
    else:
        code = scan_history()
    if code == 0:
        print("secret scan: clean")
    return code


if __name__ == "__main__":
    sys.exit(main())

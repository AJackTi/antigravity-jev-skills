#!/usr/bin/env python3
"""Build a compact, bounded worker packet without shell interpolation."""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help="one bounded objective")
    parser.add_argument("files", nargs="*", help="declared read/write scope")
    parser.add_argument("--workflow", default="discover repository instructions, exact files, and tests")
    parser.add_argument("--verify", default="use the smallest relevant test, then required repository gates")
    parser.add_argument("--output", type=pathlib.Path, help="write packet here instead of stdout")
    return parser.parse_args()


def render(args: argparse.Namespace) -> str:
    protected = any(
        item == package or item.startswith(package + "/")
        for item in args.files
        for package in ("bot", "strategies", "exchange")
    )
    files = " ".join(args.files) or "discover exact files, then keep one module seam"
    context = ["AGENTS.md", ".ai/MEMORY.md", ".ai/CODEMAP.md"]
    if protected:
        context += ["FIXES_REGISTRY.md", ".agent/workflows/critical-bugs.md"]
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    lines = [
        "# Antigravity task packet",
        f"Generated: {today}",
        "",
        "## Objective",
        args.task.strip(),
        "",
        "## Scope",
        f"Files: {files}",
        f"Workflow: {args.workflow}",
        "",
        "## Mandatory context",
        *(f"- {item}" for item in context),
        "",
        "## Worker contract",
        "Reproduce first. Edit only the declared seam. Add a regression test.",
        "Do not change credentials, live config, leverage, margin, endpoints,",
        "or safety gates without explicit user authorization.",
        "Return: Understanding, Plan (<=5 steps), Files, Change, Verification, Residual risk.",
        "",
        "## Verification",
        args.verify,
        "Also run: git diff --check and report command exit codes.",
        "",
        "## Stop conditions",
        "Stop as BLOCKED when authorization, deterministic data, or a required test is missing.",
        "After two unsuccessful review loops, escalate instead of expanding scope silently.",
        "",
        "## Reviewer contract",
        "Use a fresh context with this packet, the focused diff, and test output.",
        "Check negative paths, scope, shared state, secrets, and unsupported PASS claims.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    packet = render(args)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(packet, encoding="utf-8")
        print(f"Generated {args.output}")
    else:
        sys.stdout.write(packet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Documentation/link checker for `docs/runbooks/` (plan todo 8).

Two checks, both purely static (no network fetch — see below for why):

1. Every relative markdown link between files in `docs/runbooks/` (e.g.
   `[mcp-unavailable.md](mcp-unavailable.md)`) resolves to a file that
   actually exists on disk.
2. Every `https://runbooks.example.internal/mcp/<slug>` reference (the
   placeholder `runbook_url` host every `signoz_alert` resource in
   `infra/superapp/modules/deployments/signoz-resilience-alerts.tf` carries —
   see `docs/runbooks/README.md#what-linked-from-signoz-rules-means-here-precisely`)
   has a `<slug>` matching one of the five `RequiredSignal.runbook_url`
   entries in `resilience_signals.py`, and every required slug is
   referenced by at least one runbook.

`https://runbooks.example.internal/...` is a placeholder hostname that
resolves nowhere by design (there is no runbook-hosting service in this
stack yet) — this script deliberately never issues an HTTP request for it or
for any other external link (e.g. Argo Rollouts' real docs site). Fetching
placeholder URLs would always "fail" in a way that looks like a broken link
but isn't one, and this task's REQUIRED TOOLS exclude live network calls as
a verification method; external links are reported as "not checked", not
silently ignored.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resilience_signals import (  # noqa: E402
    PLACEHOLDER_RUNBOOK_HOST,
    REQUIRED_SIGNALS,
    required_signal_for_slug,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS_DIR = REPO_ROOT / "docs" / "runbooks"

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# The placeholder runbook_url slugs are cited in these docs as inline code
# spans (e.g. `` `https://runbooks.example.internal/mcp/workload-unavailable` ``),
# not as clickable markdown links — a plain markdown-link regex would never
# see them. Matched separately, directly against the raw text.
PLACEHOLDER_SLUG_RE = re.compile(
    r"https://" + re.escape(PLACEHOLDER_RUNBOOK_HOST) + r"/mcp/([A-Za-z0-9\-]+)"
)


def find_markdown_files() -> list[Path]:
    if not RUNBOOKS_DIR.exists():
        raise FileNotFoundError(f"runbooks directory not found: {RUNBOOKS_DIR}")
    files = sorted(RUNBOOKS_DIR.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"no markdown files found under {RUNBOOKS_DIR}")
    return files


def extract_links(text: str) -> list[str]:
    return MARKDOWN_LINK_RE.findall(text)


def check_file(path: Path) -> tuple[list[str], list[str], set[str]]:
    """Returns `(broken_relative_links, broken_slug_references,
    referenced_slugs)` for one runbook file."""
    broken_relative: list[str] = []
    broken_slugs: list[str] = []
    referenced_slugs: set[str] = set()

    text = path.read_text(encoding="utf-8")

    for slug in PLACEHOLDER_SLUG_RE.findall(text):
        referenced_slugs.add(slug)
        if required_signal_for_slug(slug) is None:
            broken_slugs.append(
                f"{path.name}: slug {slug!r} does not match any "
                "RequiredSignal.runbook_url in resilience_signals.py"
            )

    for target in extract_links(text):
        parsed = urlparse(target)
        if parsed.scheme in ("http", "https"):
            # Placeholder-host URLs are matched above (they may appear as
            # inline code, not just markdown links); any other external
            # host (real Argo Rollouts docs, this repo's own future GitHub
            # URL) is intentionally not checked — see this module's
            # docstring.
            continue

        # Relative link: strip any #fragment before checking existence.
        file_part = target.split("#", 1)[0]
        if not file_part:
            continue  # pure in-page anchor, e.g. "#detection"
        resolved = (path.parent / file_part).resolve()
        if not resolved.exists():
            broken_relative.append(
                f"{path.name}: relative link {target!r} does not resolve "
                f"(looked for {resolved})"
            )

    return broken_relative, broken_slugs, referenced_slugs


def run() -> int:
    files = find_markdown_files()

    all_broken_relative: list[str] = []
    all_broken_slugs: list[str] = []
    all_referenced_slugs: set[str] = set()

    for path in files:
        broken_relative, broken_slugs, referenced_slugs = check_file(path)
        all_broken_relative += broken_relative
        all_broken_slugs += broken_slugs
        all_referenced_slugs |= referenced_slugs

    required_slugs = {
        s.runbook_url.rsplit("/", 1)[-1] for s in REQUIRED_SIGNALS if s.runbook_url
    }
    unreferenced_required_slugs = sorted(required_slugs - all_referenced_slugs)

    print(f"Scanned {len(files)} runbook file(s) under {RUNBOOKS_DIR}")
    print(
        f"  {len(required_slugs)} required runbook_url slugs "
        f"(from resilience_signals.REQUIRED_SIGNALS)"
    )
    print(f"  {len(all_referenced_slugs)} distinct slug(s) referenced in docs")
    print()

    ok = True
    if all_broken_relative:
        ok = False
        print(f"BROKEN RELATIVE LINKS ({len(all_broken_relative)}):")
        for msg in all_broken_relative:
            print(f"  MISSING: {msg}")
    if all_broken_slugs:
        ok = False
        print(f"UNKNOWN SLUG REFERENCES ({len(all_broken_slugs)}):")
        for msg in all_broken_slugs:
            print(f"  MISSING: {msg}")
    if unreferenced_required_slugs:
        ok = False
        print(f"REQUIRED SLUGS NEVER REFERENCED ({len(unreferenced_required_slugs)}):")
        for slug in unreferenced_required_slugs:
            print(
                f"  MISSING: slug {slug!r} is a RequiredSignal.runbook_url "
                "but no runbook document references it"
            )

    if ok:
        print(
            "PASS — all relative links resolve and all placeholder-host "
            "slug references match a required signal."
        )
        print(
            "Note: external links (real Argo Rollouts docs, etc.) are "
            "reported as skipped, not verified — this checker never issues "
            "an HTTP request. See this script's module docstring."
        )
        return 0
    return 1


def main() -> int:
    try:
        return run()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
# `make site-check`: the whole gate for the public site, in order. Any failing step stops the run. Needs `make site-install` (and, for the browser step, Chromium:
# Playwright's own, or CHROMIUM_PATH pointing at an installed one). Nothing here needs the stack except `tapes-check --rerecord`, which is not part of this gate.
set -euo pipefail
cd "$(dirname "$0")/../.."
UV="${UV:-$(command -v uv || echo "$PWD/.tools/uvenv/bin/uv")}"
RUN="$UV run"
step() { printf '\n== %s\n' "$*"; }

step "generate the imported docs, the reference and the proof numbers"
$RUN python -m scripts.site.gen_all

step "Python tests: generation is deterministic, nothing private is published, links resolve, the broker's rules hold"
$RUN pytest tests/site tests/sandbox -q --ignore=tests/sandbox/test_broker_stack.py

step "snippets: every make target, module and API route named in the docs exists"
$RUN python -m scripts.site.snippets

step "tapes: valid, no key-shaped strings, narration matches"
$RUN python -m tools.replay_capture.check

step "types (astro check) and unit tests (vitest)"
(cd apps/site && npm run --silent typecheck && npm test --silent)

step "build"
(cd apps/site && npm run --silent build)

step "internal links in the built site"
$RUN python -m scripts.site.check_links

step "page-weight budgets"
$RUN python -m scripts.site.budgets

# a machine whose Playwright browser version does not match may still have another Chromium installed: use the newest one rather than fail on a download
if [ -z "${CHROMIUM_PATH:-}" ] && ! (cd apps/site && npx playwright install --dry-run chromium >/dev/null 2>&1 && ls "${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"/chromium_headless_shell-*/ >/dev/null 2>&1 && node -e "const {chromium}=require('@playwright/test');process.exit(require('fs').existsSync(chromium.executablePath())?0:1)" 2>/dev/null); then
  CHROMIUM_PATH="$(ls -d "${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"/chromium-*/chrome-linux64/chrome 2>/dev/null | sort -V | tail -1 || true)"
  export CHROMIUM_PATH
fi

step "browser: every page loads with no console errors and no serious accessibility problems; the playgrounds work"
(cd apps/site && npx playwright test)

printf '\nsite-check: OK\n'

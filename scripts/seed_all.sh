#!/usr/bin/env bash
# Run the full civic-literacy MVP catch-up seed sequence against prod.
#
# Each underlying seed script is an idempotent UPSERT — re-running this
# wrapper is safe; unchanged rows are no-ops.
#
# Usage:
#   ./scripts/seed_all.sh                 # full sequence with prompt before alias seed
#   ./scripts/seed_all.sh --dry-run-only  # validate every CSV; skip prod writes
#   ./scripts/seed_all.sh --skip-aliases  # skip Phase 2.A.3 alias step entirely
#   ./scripts/seed_all.sh --help          # this message
#
# Order of operations:
#   1. Dry-run pass — every seed script with --dry-run; aborts on any failure
#   2. seed_outlet_profiles      (Phase 2.A)
#   3. audit_source_aliases      (Phase 2.A.3) — generates suggestions CSV
#   4. PAUSE — user reviews data/source_alias_suggestions.csv
#   5. seed_source_aliases       (Phase 2.A.3, gated on user confirmation)
#   6. seed_politician_profiles  (Phase 3.A)
#   7. seed_org_profiles         (Phase 3.A)
#   8. seed_bill_profiles        (Phase 3.A)

set -euo pipefail

# ─── argv parsing ──────────────────────────────────────────
DRY_RUN_ONLY=false
SKIP_ALIASES=false

for arg in "$@"; do
  case "$arg" in
    --dry-run-only) DRY_RUN_ONLY=true ;;
    --skip-aliases) SKIP_ALIASES=true ;;
    -h|--help)
      # Print the leading comment block as help text.
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2
      echo "Run with --help for usage." >&2
      exit 2
      ;;
  esac
done

# ─── env checks ────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIFT_API_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SIFT_API_ROOT"

PYTHON="./.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: $PYTHON not found. Run from sift-api root with the .venv set up." >&2
  exit 1
fi

if ! command -v railway >/dev/null 2>&1; then
  echo "ERROR: 'railway' CLI not on PATH." >&2
  echo "Install via https://docs.railway.app/develop/cli." >&2
  exit 1
fi

# ─── output styling ────────────────────────────────────────
if [[ -t 1 ]]; then
  C_HEAD=$'\033[1;36m'   # bold cyan
  C_STEP=$'\033[1;33m'   # bold yellow
  C_OK=$'\033[1;32m'     # bold green
  C_WARN=$'\033[1;31m'   # bold red
  C_OFF=$'\033[0m'
else
  C_HEAD=""; C_STEP=""; C_OK=""; C_WARN=""; C_OFF=""
fi

heading() { printf '\n%s━━━ %s ━━━%s\n' "$C_HEAD" "$1" "$C_OFF"; }
step()    { printf '%s→ %s%s\n'         "$C_STEP" "$1" "$C_OFF"; }
ok()      { printf '%s✓ %s%s\n'         "$C_OK"   "$1" "$C_OFF"; }
warn()    { printf '%s! %s%s\n'         "$C_WARN" "$1" "$C_OFF"; }

# ─── 1. dry-run validation ─────────────────────────────────
heading "Validating CSVs (dry-run, no DB writes)"
for s in seed_outlet_profiles seed_politician_profiles seed_org_profiles seed_bill_profiles; do
  step "$s --dry-run"
  railway run "$PYTHON" "scripts/$s.py" --dry-run
done
ok "All CSVs parse clean."

if [[ "$DRY_RUN_ONLY" == "true" ]]; then
  printf '\n'
  ok "Dry-run complete (--dry-run-only); no prod writes performed."
  exit 0
fi

# ─── 2. Phase 2.A — outlet_profiles ────────────────────────
heading "Phase 2.A — outlet_profiles"
railway run "$PYTHON" scripts/seed_outlet_profiles.py
ok "outlet_profiles synced."

# ─── 3. Phase 2.A.3 — alias audit + seed (with review pause) ─
if [[ "$SKIP_ALIASES" == "false" ]]; then
  heading "Phase 2.A.3 — source_name aliases"

  step "audit_source_aliases (writes data/source_alias_suggestions.csv)"
  railway run "$PYTHON" scripts/audit_source_aliases.py

  printf '\n'
  warn "Review data/source_alias_suggestions.csv before continuing:"
  echo "  - 'exact'      rows are high-confidence — leave alone"
  echo "  - 'substring'  rows need a human eye — fix or drop"
  echo "  - 'none'       rows have no suggestion; fill in or leave empty"
  echo "  - Empty 'suggested_outlet_slug' rows are skipped on seed"
  printf '\n'

  read -r -p "Apply the reviewed CSV to source_name_aliases? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    railway run "$PYTHON" scripts/seed_source_aliases.py \
      --input data/source_alias_suggestions.csv
    ok "source_name_aliases synced."
  else
    warn "Skipped seed_source_aliases."
    warn "Apply manually later when ready:"
    echo "  railway run $PYTHON scripts/seed_source_aliases.py --input data/source_alias_suggestions.csv"
  fi
else
  warn "Skipping Phase 2.A.3 alias step (--skip-aliases)."
fi

# ─── 4. Phase 3.A — politician / org / bill profiles ──────
heading "Phase 3.A — politician/org/bill profiles"
for s in seed_politician_profiles seed_org_profiles seed_bill_profiles; do
  step "$s"
  railway run "$PYTHON" "scripts/$s.py"
done
ok "All Phase 3.A tables synced."

# ─── done ──────────────────────────────────────────────────
printf '\n'
ok "Catch-up complete. Spot-check via the sift dev preview:"
echo "  - feed → click an outlet name → /outlet/[slug] dossier"
echo "  - /methodology → live outlet list grouped Left / Center / Right / Unrated"
echo "  - /politician/S000148 → Schumer dossier (once Phase 3.C ships)"

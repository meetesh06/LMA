#!/usr/bin/env bash
# Remove Debian build output for LMA. Only known build artifacts are touched.
#   ./clean.sh            delete them
#   ./clean.sh --dry-run  only list what would be deleted
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC="$ROOT/lma-src"
DRY=0
case "${1:-}" in
  "") ;;
  -n|--dry-run) DRY=1 ;;
  -h|--help) sed -n '2,4s/^# \{0,1\}//p' "$0"; exit 0 ;;
  *) echo "usage: ./clean.sh [--dry-run]" >&2; exit 2 ;;
esac

shopt -s nullglob
targets=(
  # dpkg-buildpackage / debuild / dput output, written next to lma-src/
  "$ROOT"/lma_*.deb "$ROOT"/lma_*.ddeb "$ROOT"/lma_*.dsc "$ROOT"/lma_*.tar.* "$ROOT"/lma_*.diff.gz
  "$ROOT"/lma_*.changes "$ROOT"/lma_*.buildinfo "$ROOT"/lma_*.build "$ROOT"/lma_*.upload
  # debhelper state inside the source tree
  "$SRC"/debian/lma "$SRC"/debian/.debhelper "$SRC"/debian/files "$SRC"/debian/debhelper-build-stamp
  "$SRC"/debian/*.substvars "$SRC"/debian/*.debhelper.log "$SRC"/debian/*.debhelper
  # Python bytecode from local test runs
  "$SRC"/src/lib/__pycache__
)

if [ ${#targets[@]} -eq 0 ]; then
  echo "Nothing to clean."
  exit 0
fi

for t in "${targets[@]}"; do
  if [ $DRY -eq 1 ]; then echo "would remove  ${t#"$ROOT"/}"; else echo "removing      ${t#"$ROOT"/}"; fi
  [ $DRY -eq 1 ] || rm -rf -- "$t"
done

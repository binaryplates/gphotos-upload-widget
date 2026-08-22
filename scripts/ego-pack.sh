#!/usr/bin/env bash
# Build an extensions.gnome.org upload zip (extension files at archive root).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
UUID="gphotos-upload@blazorplate.net"
SRC="$REPO_ROOT/$UUID"
VERSION="$(python3 -c "import json; print(json.load(open('$SRC/metadata.json'))['version'])")"
OUT_DIR="$REPO_ROOT/dist/ego"
OUT="$OUT_DIR/${UUID}-v${VERSION}.zip"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp -a "$SRC"/. "$STAGE/"
rm -rf "$STAGE/backend"
cp -a "$REPO_ROOT/backend/." "$STAGE/backend/"
install -m 0755 "$SRC/backend/run-setup.sh" "$STAGE/backend/run-setup.sh"
rm -rf "$STAGE/backend/build" "$STAGE/backend/"*.egg-info "$STAGE/backend/**/__pycache__" 2>/dev/null || true
find "$STAGE/backend" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
glib-compile-schemas "$STAGE/schemas"

(
  cd "$STAGE"
  zip -qr "$OUT" . \
    -x '*.git*' \
    -x '*~' \
    -x '*.swp' \
    -x '*.zip'
)

echo "wrote $OUT"
unzip -l "$OUT" | head -25

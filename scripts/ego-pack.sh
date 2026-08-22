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

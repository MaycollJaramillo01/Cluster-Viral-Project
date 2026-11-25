#!/bin/bash
# Build "PurffleShorts Studio.app" and a drag-to-Applications .dmg for this Mac's CPU (arm64 or x86_64).
#
#   packaging/macos/build_dmg.sh     ->  dist/PurffleShorts-Studio-<version>-macOS-<AppleSilicon|Intel>.dmg
#
# Reproducible inputs: the Python given in $PYTHON (CI pins 3.12), every package pinned by hash in
# requirements.lock, timestamps from the last commit. The app carries its own Python, packages and ffmpeg
# (imageio-ffmpeg), so the Mac it is installed on needs nothing else. The build stops if any secret-looking
# file or value ends up in the app.
set -euo pipefail
if [ "$(uname -s)" != Darwin ]; then
    echo "This builds a macOS app, so it only runs on a Mac (PyInstaller can't build for macOS elsewhere)."
    echo "Copy this repository to a Mac and run: packaging/macos/build_dmg.sh"
    exit 1
fi
cd "$(dirname "$0")/../.."
PY=${PYTHON:-python3}
ARCH=$(uname -m)
KIND=$([ "$ARCH" = arm64 ] && echo AppleSilicon || echo Intel)
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' purffle_shorts/__init__.py)
APP="dist/PurffleShorts Studio.app"
DMG="dist/PurffleShorts-Studio-$VERSION-macOS-$KIND.dmg"
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}
export PYTHONHASHSEED=0 PIP_DISABLE_PIP_VERSION_CHECK=1

echo "== Python environment ($("$PY" --version), $ARCH)"
rm -rf build dist .build-venv
"$PY" -m venv .build-venv
# shellcheck disable=SC1091
. .build-venv/bin/activate
python -m pip install --quiet --require-hashes --only-binary :all: --no-deps -r packaging/macos/requirements.lock
python -m pip install --quiet --no-deps --no-build-isolation .
python -m pip check

echo "== App bundle"
pyinstaller packaging/purffle-studio.spec --noconfirm --clean --log-level WARN
codesign --force --deep --sign - "$APP"        # ad-hoc: Apple silicon runs no unsigned code
codesign --verify --deep --strict "$APP"
FFMPEG=$(find "$APP" -type f -name 'ffmpeg-*' -perm -u+x | head -1)
[ -n "$FFMPEG" ] && "$FFMPEG" -hide_banner -version | head -1 || { echo "ffmpeg is missing from the app"; exit 1; }
lipo -archs "$APP/Contents/MacOS/PurffleShorts" | grep -qw "$ARCH"

echo "== No secrets in the app"
if find "$APP" \( -name '.env' -o -name '*.env' -o -name 'credentials.json' -o -name 'token.json' \
        -o -name 'client_secret*.json' -o -name '*.pickle' -o -name '.keychain-names' \) | grep .; then
    echo "credential files found in the app"; exit 1
fi
KEYS='sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|gsk_[A-Za-z0-9]{40,}|xai-[A-Za-z0-9]{40,}|ghp_[A-Za-z0-9]{36}|ya29\.[0-9A-Za-z_-]{20,}|1//0[0-9A-Za-z_-]{30,}'
# the app's own files, and the package sources it was built from (the app keeps them compressed)
if grep -raEl "$KEYS" "$APP" purffle_shorts packaging pyproject.toml; then
    echo "a value that looks like an API key or token is in the app"; exit 1
fi

echo "== Disk image"
STAGE=$(mktemp -d)
ditto "$APP" "$STAGE/PurffleShorts Studio.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "PurffleShorts Studio" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG"
rm -rf "$STAGE"
(cd dist && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256")
echo "Built $DMG"
cat "$DMG.sha256"

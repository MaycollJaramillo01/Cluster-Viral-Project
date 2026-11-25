#!/bin/bash
# End-to-end check of a built .dmg on a Mac, as a user would go: install from the disk image, import a .env
# written on Windows, open the app from /Applications (with port 8765 taken), open it a second time, render a
# video, save a key from the Settings page, replace the app and check nothing was lost.
#
#   packaging/macos/verify.sh dist/PurffleShorts-Studio-<version>-macOS-<kind>.dmg
#
# Uses throwaway values only (the Gemini key is fake, so Gemini answers "invalid key"). It writes to
# ~/Library/Application Support/PurffleShorts and the Keychain: run it on a CI runner, not on your own Mac.
set -euo pipefail
DMG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
APP="/Applications/PurffleShorts Studio.app"
HOME_DIR="$HOME/Library/Application Support/PurffleShorts"
MOVIES="$HOME/Movies/PurffleShorts"
FAKE="AIzaCI-fake-$(openssl rand -hex 14)"
WORK=$(mktemp -d)
PASSED=()
step() { echo; echo "::group::$*"; }
pass() { PASSED+=("$*"); echo "PASS: $*"; echo "::endgroup::"; }
fail() { echo "FAIL: $*"; echo "--- desktop.log"; tail -50 "$HOME_DIR/logs/desktop.log" 2>/dev/null || true; exit 1; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
studio_pid() { json 'd["pid"]' < "$HOME_DIR/.studio.json"; }
studio_port() { json 'd["port"]' < "$HOME_DIR/.studio.json"; }
api() { curl -sf "http://127.0.0.1:$PORT$1" "${@:2}"; }
post() { api "$1" -X POST -H "X-Studio-Token: $TOKEN" -H "Content-Type: application/json" -d "$2"; }
install_app() {
    MNT=$(hdiutil attach -nobrowse -readonly "$DMG" | awk -F'\t' '/\/Volumes\//{print $NF}')
    [ -d "$MNT/PurffleShorts Studio.app" ] && [ -L "$MNT/Applications" ] || fail "the disk image layout"
    rm -rf "$APP"
    ditto "$MNT/PurffleShorts Studio.app" "$APP"   # what dragging to Applications does
    hdiutil detach "$MNT" -quiet
}
wait_studio() {  # until the Studio the app started answers
    for _ in $(seq 1 120); do
        if [ -f "$HOME_DIR/.studio.json" ]; then
            PORT=$(studio_port)
            api /api/info > /dev/null 2>&1 && return 0
        fi
        sleep 1
    done
    fail "the Studio did not answer within 2 minutes"
}
stop_studio() { kill "$(studio_pid)" 2>/dev/null || true; sleep 2; }
trap 'stop_studio 2>/dev/null; kill ${BUSY:-} 2>/dev/null || true' EXIT

step "Install from the disk image"
rm -rf "$HOME_DIR" "$MOVIES"
install_app
codesign --verify --deep --strict "$APP" || fail "signature"
pass "installed $(basename "$DMG") into /Applications; signature intact"

step "Import a .env written on Windows"
mkdir -p "$WORK/from-windows"
cat > "$WORK/from-windows/.env" <<EOF
# my settings
LLM_PROVIDER=gemini
LLM_MODEL=gemini-2.5-flash
GEMINI_API_KEY=$FAKE
OPENAI_API_KEY=
SCRIPT_REVIEW=rewrite
CAPTION_STYLE=karaoke
TTS_ENGINE=edge
GOOGLE_CLIENT_SECRETS_FILE=C:\\Users\\me\\purffle-shorts\\credentials.json
MEDIA_DIR=D:\\clips\\media
OUTPUT_DIR=output_videos
DATA_DIR=data
EOF
perl -pi -e 's/\n/\r\n/' "$WORK/from-windows/.env"           # Windows line endings
echo '{"installed": {"client_id": "ci-placeholder"}}' > "$WORK/from-windows/credentials.json"
"$APP/Contents/MacOS/PurffleShorts" --import-env "$WORK/from-windows/.env" || fail "import exited with an error"
ENV="$HOME_DIR/.env"
[ "$(stat -f %Lp "$ENV")" = 600 ] || fail ".env is not private"
grep -qx 'LLM_PROVIDER=gemini' "$ENV" && grep -qx 'LLM_MODEL=gemini-2.5-flash' "$ENV" \
    && grep -qx 'CAPTION_STYLE=karaoke' "$ENV" || fail "settings were not kept"
grep -qx 'GOOGLE_CLIENT_SECRETS_FILE=credentials.json' "$ENV" || fail "OAuth file path not fixed"
[ "$(stat -f %Lp "$HOME_DIR/credentials.json")" = 600 ] || fail "credentials.json not copied privately"
! grep -q '^MEDIA_DIR=' "$ENV" || fail "Windows MEDIA_DIR kept"
grep -qx "OUTPUT_DIR=$MOVIES" "$ENV" || fail "OUTPUT_DIR not moved to ~/Movies"
if [ "$(security find-generic-password -s com.purffle.shorts -a GEMINI_API_KEY -w 2>/dev/null)" = "$FAKE" ]; then
    ! grep -qF "$FAKE" "$ENV" || fail "the key is in the Keychain and in .env"
    WHERE="the macOS Keychain"
else
    grep -qF "$FAKE" "$ENV" || fail "the key is neither in the Keychain nor in .env"
    WHERE="the private .env (the Keychain refused it on this machine)"
fi
pass "imported: settings kept, Windows paths fixed, credentials.json copied (600), GEMINI_API_KEY in $WHERE"

step "Open from Applications with port 8765 taken"
python3 -m http.server 8765 --bind 127.0.0.1 > /dev/null 2>&1 & BUSY=$!
sleep 1
open "$APP"
wait_studio
[ "$PORT" != 8765 ] || fail "used the taken port"
INFO=$(api /api/info)
echo "$INFO" | json 'd["llm"], d["llm_ok"], d["caption_style"], d["review"]'
[ "$(echo "$INFO" | json 'd["llm"]')" = "gemini:gemini-2.5-flash" ] || fail "Gemini settings not used"
[ "$(echo "$INFO" | json 'd["llm_ok"]')" = "True" ] || fail "Gemini key not found"
pass "Studio answers on 127.0.0.1:$PORT with Gemini (gemini-2.5-flash) configured"

step "Open it again: the running Studio is reused"
PID=$(studio_pid)
open "$APP"
sleep 8
[ "$(studio_pid)" = "$PID" ] && [ "$(studio_port)" = "$PORT" ] || fail "a second Studio was started"
[ "$(pgrep -f 'PurffleShorts.*--serve' | wc -l | tr -d ' ')" = 1 ] || fail "more than one Studio process"
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | grep -q '127.0.0.1' || fail "not on loopback"
pass "second launch reused PID $PID; listens on 127.0.0.1 only"

TOKEN=$(api / | sed -n 's/.*name="studio-token" content="\([^"]*\)".*/\1/p')
[ -n "$TOKEN" ] || fail "no Studio token"
wait_job() {  # prints the finished job as JSON
    for _ in $(seq 1 900); do
        J=$(api "/api/jobs/$1")
        case "$(echo "$J" | json 'd["status"]')" in done|failed|cancelled) echo "$J"; return 0;; esac
        sleep 2
    done
    fail "job $1 did not finish in 30 minutes"
}

step "A Gemini failure names the provider and hides the key"
JOB=$(post /api/draft '{"topic": "why octopuses have three hearts"}' | json 'd["job"]')
J=$(wait_job "$JOB")
echo "$J" | json 'd["status"], (d["result"] or {}).get("error", "")[:400]'
[ "$(echo "$J" | json 'd["status"]')" = failed ] || fail "a fake Gemini key should fail"
echo "$J" | grep -qi gemini || fail "the error does not say Gemini"
! echo "$J" | grep -qF "$FAKE" || fail "the key is in the job output"
pass "draft with the fake key failed and the error names gemini, without the key"

step "Render a test video (offline script, free voice, generated visuals)"
JOB=$(post /api/make '{"offline": true, "topic": "why octopuses have three hearts", "duration": 15, "music": false}' \
      | json 'd["job"]')
J=$(wait_job "$JOB")
[ "$(echo "$J" | json 'd["status"]')" = done ] || { echo "$J" | json 'd["log"][-30:]'; fail "render failed"; }
VID=$(echo "$J" | json 'd["result"]["id"]')
MP4=$(find "$MOVIES" -name '*.mp4' -size +50k | head -1)
[ -n "$MP4" ] || fail "no MP4 in ~/Movies/PurffleShorts"
FFMPEG=$(find "$APP" -type f -name 'ffmpeg-*' -perm -u+x | head -1)
PROBE=$("$FFMPEG" -hide_banner -i "$MP4" 2>&1 || true)
echo "$PROBE" | grep -E 'Duration|Stream'
echo "$PROBE" | grep -q 'Video: h264' && echo "$PROBE" | grep -q 'Audio: aac' || fail "the MP4 lacks H.264 video or AAC audio"
api "/files/$VID/$(basename "$MP4")" -o "$WORK/served.mp4" -r 0-1023 || fail "the Studio does not serve the video"
pass "rendered #$VID: $(basename "$MP4") ($(du -h "$MP4" | cut -f1)), H.264 + AAC, served by the Studio"

step "Save a key from the Settings page"
SAVED=$(post /api/settings '{"values": {"PEXELS_API_KEY": "pexels-ci-placeholder-0123456789"}}')
echo "$SAVED" | grep -q 'pexels-ci-placeholder' && fail "the Settings page sent the key back"
if [ "$WHERE" = "the macOS Keychain" ]; then
    [ "$(security find-generic-password -s com.purffle.shorts -a PEXELS_API_KEY -w)" = pexels-ci-placeholder-0123456789 ] \
        || fail "Settings key not in the Keychain"
    ! grep -q 'pexels-ci-placeholder' "$ENV" || fail "Settings key written to .env"
fi
pass "a key saved from Settings went to $WHERE and was shown masked"

step "Replace the app: settings, keys and videos stay"
stop_studio
install_app
open "$APP"
wait_studio
[ "$(api /api/info | json 'd["llm"]')" = "gemini:gemini-2.5-flash" ] || fail "settings lost"
[ "$(api /api/videos | json 'len(d)')" -ge 1 ] || fail "library lost"
[ -f "$MP4" ] || fail "video lost"
pass "after reinstalling, Gemini settings, the key and the library are still there"

step "No secret in logs, settings, videos or API answers"
SCAN=("$HOME_DIR" "$MOVIES")
LEAK=$(grep -rlF "$FAKE" "${SCAN[@]}" 2>/dev/null | grep -v "^$ENV$" || true)
[ -z "$LEAK" ] || fail "the key leaked into: $LEAK"
for p in /api/jobs /api/info /api/settings /api/doctor; do ! api "$p" | grep -qF "$FAKE" || fail "the key is in $p"; done
ls -la "$HOME_DIR" "$HOME_DIR/logs"
pass "the fake key appears in no log, video folder or API answer"

echo
echo "All checks passed ($(uname -m), macOS $(sw_vers -productVersion)):"
printf ' - %s\n' "${PASSED[@]}"

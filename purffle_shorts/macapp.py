"""What the macOS app (PurffleShorts Studio.app) adds to the desktop launcher.

* Each double-click runs this launcher again: it starts the Studio in the background (or finds the one already
  running), opens it in the default browser and exits, so the app never sits in the Dock.
* First launch: offers to import an existing .env. API keys and tokens go to the macOS Keychain (or, if the
  Keychain can't be used, stay in the private .env, mode 600); Windows paths are fixed or asked for again.
* Hold Option while opening the app for: import a .env again, choose the YouTube OAuth file, open the data
  folder, stop the Studio.

Dialogs use osascript, the Keychain /usr/bin/security, so the app needs no extra Python packages.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path, PureWindowsPath

from . import envfile

TITLE = "PurffleShorts Studio"
SERVICE = "com.purffle.shorts"  # Keychain service name (no spaces: it is passed to `security -i` unquoted)

# Files the app copies into its own folder (credentials must not stay in Downloads or a synced folder).
CREDENTIAL_FILES = {"GOOGLE_CLIENT_SECRETS_FILE": "credentials.json", "YT_TOKEN_FILE": "token.json"}
# Paths from another computer that are fixed or asked for again: key -> "file" | "folder" | None (just drop it)
PATH_KEYS = {"BACKGROUND_VIDEO": "file", "CAPTION_FONT": "file", "TOPICS_FILE": "file", "MEDIA_DIR": "folder",
             "MUSIC_DIR": "folder", "DATA_DIR": None, "FFMPEG_BINARY": None}


def movies_dir() -> Path:
    return Path.home() / "Movies" / "PurffleShorts"


# ------------------------------------------------------------------ Keychain
def keychain_enabled() -> bool:
    return sys.platform == "darwin" and os.getenv("PURFFLE_KEYCHAIN") == "1"


def _security(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["/usr/bin/security", *args], input=stdin, capture_output=True, text=True, timeout=30)


def _index() -> Path:
    return Path(os.getenv("PURFFLE_HOME") or ".") / ".keychain-names"


def keychain_names() -> list[str]:
    try:
        return [n for n in json.loads(_index().read_text(encoding="utf-8")) if envfile.KEY_RE.match(f"{n}=")]
    except (OSError, ValueError, TypeError):
        return []


def _save_names(names) -> None:
    _index().write_text(json.dumps(sorted(set(names))), encoding="utf-8")


def keychain_get(name: str) -> str | None:
    r = _security("find-generic-password", "-s", SERVICE, "-a", name, "-w")
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def keychain_set(name: str, value: str) -> bool:
    """Store a secret; True once it reads back. The value goes in as hex on stdin, never on a command line."""
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        return False
    _security("-i", stdin=f"add-generic-password -U -s {SERVICE} -a {name} -X {value.encode().hex()}\n")
    if keychain_get(name) != value:
        return False
    _save_names([*keychain_names(), name])
    return True


def keychain_delete(name: str) -> None:
    _security("delete-generic-password", "-s", SERVICE, "-a", name)
    _save_names(n for n in keychain_names() if n != name)


def load_keychain() -> list[str]:
    """Put the stored secrets in the environment (they win over .env); returns the names loaded."""
    loaded = []
    for name in keychain_names():
        value = keychain_get(name)
        if value:
            os.environ[name] = value
            loaded.append(name)
    return loaded


# ------------------------------------------------------------------ dialogs
def _osa(script: str, *args: str) -> str | None:
    """Run AppleScript with string arguments (never pasted into the script); None when the user cancels."""
    r = subprocess.run(["osascript", "-e", f"on run argv\nactivate\n{script}\nend run", *args],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def ask(message: str, buttons: list[str]) -> str | None:
    """Show a dialog; the last button is the default. Returns the button clicked."""
    return _osa(f'button returned of (display dialog (item 1 of argv) with title "{TITLE}" '
                "buttons (items 2 thru -1 of argv) default button ((count of argv) - 1))", message, *buttons)


def tell(message: str) -> None:
    _osa(f'display dialog (item 1 of argv) with title "{TITLE}" buttons {{"OK"}} default button 1', message)


def choose(prompt: str, kind: str = "file") -> Path | None:
    script = ("POSIX path of (choose file with prompt (item 1 of argv) invisibles true)" if kind == "file"
              else "POSIX path of (choose folder with prompt (item 1 of argv))")
    got = _osa(script, prompt)
    return Path(got) if got else None


def option_held() -> bool:
    """Is the Option key down right now (the user opened the app with Option held)?"""
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGEventSourceFlagsState.restype = ctypes.c_uint64
        cg.CGEventSourceFlagsState.argtypes = [ctypes.c_int32]
        return bool(cg.CGEventSourceFlagsState(0) & 0x80000)  # combined session state, kCGEventFlagMaskAlternate
    except (OSError, AttributeError):
        return False


# ------------------------------------------------------------------ .env import
def is_windows_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith("\\\\") or "\\" in value


def _locate(value: str, base: Path) -> Path | None:
    """The file a setting points at, on this Mac: the path itself, or a file of the same name next to the .env."""
    if is_windows_path(value):
        candidates = [base / PureWindowsPath(value).name]
    else:
        p = Path(value).expanduser()
        candidates = [p if p.is_absolute() else base / p]
    return next((c for c in candidates if c.exists()), None)


def _private_copy(src: Path, dest: Path) -> None:
    if src.resolve() != dest.resolve():
        shutil.copyfile(src, dest)
    os.chmod(dest, 0o600)


def import_env(src: Path, home: Path, interactive: bool = True) -> list[str]:
    """Make the .env at ``src`` this app's settings. Returns a report (setting names, never values)."""
    text = src.read_text(encoding="utf-8-sig")
    values = envfile.parse(text)
    secrets = {k: v for k, v in values.items() if envfile.is_secret(k) and v}
    updates: dict[str, str | None] = dict(secrets)
    report: list[str] = []

    for key, name in CREDENTIAL_FILES.items():
        given = values.get(key, "")
        found = _locate(given or name, src.parent)
        if not found and given and interactive and ask(
                f"{key} points to “{given}”, which isn't on this Mac. Choose the file now, or skip and choose it "
                "later (open the app with Option held).", ["Skip", "Choose…"]) == "Choose…":
            found = choose(f"Choose the file for {key}")
        if found:
            _private_copy(found, home / name)
            report.append(f"{key}: copied {found.name} into the app folder")
        elif given:
            report.append(f"{key}: “{given}” not found; choose it later (open the app with Option held)")
        if found or given:
            updates[key] = name

    for key, kind in PATH_KEYS.items():
        given = values.get(key, "")
        if not given:
            continue
        found = _locate(given, src.parent)
        if found:
            updates[key] = str(found.resolve())
            continue
        if not is_windows_path(given) and not Path(given).expanduser().is_absolute():
            continue  # a relative folder such as "media": it lives in the app folder
        if kind and interactive and ask(f"{key} points to “{given}”, which isn't on this Mac. Choose the {kind} "
                                        "on this Mac, or skip to use the default.", ["Skip", "Choose…"]) == "Choose…":
            found = choose(f"Choose the {kind} for {key}", kind)
        updates[key] = str(found) if found else None
        report.append(f"{key}: now {found}" if found else f"{key}: “{given}” isn't on this Mac; using the default")

    out = values.get("OUTPUT_DIR", "")
    if not out or is_windows_path(out) or not Path(out).expanduser().is_absolute():
        updates["OUTPUT_DIR"] = str(movies_dir())

    # The new file never holds secrets: their lines are left out, then envfile.write puts each one in the
    # Keychain (or back in this file, mode 600, if the Keychain can't take it).
    dest = home / ".env"
    kept = [line for line in text.splitlines()
            if not ((m := envfile.KEY_RE.match(line)) and envfile.is_secret(m.group(1)))]
    dest.touch(mode=0o600)
    os.chmod(dest, 0o600)
    dest.write_text("\n".join(kept) + "\n", encoding="utf-8")
    if keychain_enabled():
        for old in set(keychain_names()) - set(secrets):  # importing replaces the settings
            keychain_delete(old)
    envfile.write(updates, path=dest)

    in_chain = set(keychain_names()) if keychain_enabled() else set()
    if secrets.keys() & in_chain:
        report.insert(0, "In the Keychain: " + ", ".join(sorted(secrets.keys() & in_chain)))
    if secrets.keys() - in_chain:
        report.insert(0, "In the private settings file (mode 600): " + ", ".join(sorted(secrets.keys() - in_chain)))
    report.insert(0, f"Imported {len(values)} settings from {src.name}.")
    return report


# ------------------------------------------------------------------ launcher
def _state(home: Path) -> dict:
    try:
        return json.loads((home / ".studio.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def first_run(home: Path) -> None:
    choice = ask("Welcome to PurffleShorts Studio.\n\nImport your settings from an existing .env file? API keys "
                 "are saved in your macOS Keychain, not in the app.\n\nYou can also set up AI providers later on "
                 "the Studio's Settings page.", ["Configure later", "Import .env…"])
    src = choose("Choose your .env file (press ⌘⇧. to show hidden files)") if choice == "Import .env…" else None
    if src:
        tell("\n".join(import_env(src, home)))
    elif not (home / ".env").exists():
        envfile.write({"OUTPUT_DIR": str(movies_dir())}, path=home / ".env")


def option_menu(home: Path, running: bool) -> bool:
    """The Option-click menu. Returns False when the user doesn't want the Studio opened now."""
    items = ["Import a .env file…", "Choose the YouTube OAuth file (credentials.json)…", "Open the data folder"]
    if running:
        items.append("Stop Studio")
    picked = _osa(f'choose from list (items 1 thru -1 of argv) with title "{TITLE}" '
                  'with prompt "PurffleShorts Studio settings"', *items)
    if not picked or picked == "false":
        return False
    if picked == items[0] and (src := choose("Choose your .env file (press ⌘⇧. to show hidden files)")):
        tell("\n".join(import_env(src, home)) + "\n\nRestart the Studio (Stop Studio) to use them.")
    elif picked == items[1] and (src := choose("Choose your Google OAuth client file")):
        _private_copy(src, home / "credentials.json")
        envfile.write({"GOOGLE_CLIENT_SECRETS_FILE": "credentials.json"}, path=home / ".env")
        tell("Saved. Connect YouTube from the Studio, or restart it if it is running.")
    elif picked == items[2]:
        subprocess.run(["open", str(home)])
        return False
    elif picked == "Stop Studio":
        stop(home)
        return False
    return True


def stop(home: Path) -> None:
    from .desktop import running_studio
    st = _state(home)
    if st.get("pid") and running_studio(int(st.get("port", 0))):
        try:
            os.kill(int(st["pid"]), signal.SIGTERM)
        except OSError:
            pass


def launch(home: Path, argv: list[str]) -> int:
    """A double-click: set up on first run, then open the running Studio or start one in the background."""
    from .desktop import free_port, running_studio, studio_url
    port = int(_state(home).get("port") or 0)
    running = bool(port) and running_studio(port)
    if option_held():
        if not option_menu(home, running):
            return 0
        running = bool(port) and running_studio(port)
    elif not (home / ".env").exists():
        first_run(home)
    if running:
        webbrowser.open(studio_url(port))
        return 0
    port = free_port()
    log = open(home / "logs" / "desktop.log", "ab")  # noqa: SIM115 - handed to the child
    me = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "purffle_shorts.desktop"]
    child = subprocess.Popen([*me, "--serve", "--port", str(port), "--no-browser", *argv], cwd=home,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    (home / ".studio.json").write_text(json.dumps({"port": port, "pid": child.pid}), encoding="utf-8")
    deadline = time.time() + 120
    while time.time() < deadline:
        if running_studio(port):
            webbrowser.open(studio_url(port))
            return 0
        if child.poll() is not None:
            break
        time.sleep(0.5)
    tell(f"The Studio did not start. Details are in {home / 'logs' / 'desktop.log'}")
    return 1

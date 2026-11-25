"""Double-click launcher for the desktop builds (Windows .exe, macOS .app, Linux AppImage).

What it does that ``purffle-shorts studio`` in a terminal doesn't need to:

* picks a home folder the user can find and write to (a .app starts in ``/``, an .exe wherever it was
  clicked from): ``PURFFLE_HOME`` if set, on macOS ``~/Library/Application Support/PurffleShorts``,
  otherwise ``Documents/PurffleShorts``;
* opens the Studio that is already running instead of starting a second copy;
* uses another port when 8765 is taken by some other program;
* gives a windowed build (no console, so ``sys.stdout`` is None) somewhere to print;
* keeps the values of API keys out of every log line.

On macOS the launcher starts the Studio in the background and exits (see ``macapp``).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8765
APP_DIR = "PurffleShorts"


def home_dir() -> Path:
    if os.getenv("PURFFLE_HOME"):
        return Path(os.environ["PURFFLE_HOME"]).expanduser()
    if sys.platform == "darwin":  # not Documents: macOS asks for permission there, and iCloud may sync it
        return Path.home() / "Library" / "Application Support" / APP_DIR
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / APP_DIR


def studio_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def running_studio(port: int) -> bool:
    """Is our own Studio already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=1.5) as r:
            return "llm" in json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return False


def free_port(preferred: int = PORT) -> int:
    for port in (preferred, 0):  # 0: let the system pick one
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if port and probe.connect_ex(("127.0.0.1", port)) == 0:
                continue  # something answers there (bound to 0.0.0.0, which SO_REUSEADDR would not notice)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # like the Studio's own server: a port a closed Studio left in TIME_WAIT is still ours to use
            if sys.platform != "win32":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise OSError("no free port")


def _console(home: Path) -> None:
    if sys.stdout is None or sys.stderr is None:
        out = open(home / "logs" / "desktop.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or out
        sys.stderr = sys.stderr or out


def scrub_logs() -> None:
    """Replace the value of every API key, token and password in the environment with *** in all log output
    (log file, console and the Studio's job log), whatever the message that carried it."""
    from .envfile import is_secret
    fmt = logging.Formatter.format

    def format(self, record):
        text = fmt(self, record)
        for key, value in os.environ.items():
            if len(value) >= 8 and is_secret(key) and value in text:
                text = text.replace(value, "***")
        return text

    logging.Formatter.format = format


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    home = home_dir()
    (home / "logs").mkdir(parents=True, exist_ok=True)
    _console(home)
    os.environ["PURFFLE_HOME"] = str(home)
    mac = sys.platform == "darwin"
    if mac:
        os.environ["PURFFLE_KEYCHAIN"] = "1"
        from . import macapp
        if argv[:1] == ["--import-env"] and len(argv) == 2:  # scripted import (no dialogs), used by the CI check
            print("\n".join(macapp.import_env(Path(argv[1]).expanduser(), home, interactive=False)))
            return 0
    serve = "--serve" in argv
    if serve:
        argv.remove("--serve")
        if mac:
            macapp.load_keychain()
    elif mac:
        return macapp.launch(home, argv)
    elif running_studio(PORT):
        webbrowser.open(studio_url(PORT))
        return 0
    else:
        argv = ["--port", str(free_port()), *argv]
    scrub_logs()
    from .cli import main as cli_main
    return cli_main(["studio", *argv])


if __name__ == "__main__":
    raise SystemExit(main())

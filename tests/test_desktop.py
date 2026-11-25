"""The double-click launcher used by the desktop builds."""

import socket

from purffle_shorts import desktop


def test_home_is_purffle_home_or_documents(monkeypatch, tmp_path):
    monkeypatch.setenv("PURFFLE_HOME", str(tmp_path / "mine"))
    assert desktop.home_dir() == tmp_path / "mine"
    monkeypatch.delenv("PURFFLE_HOME")
    monkeypatch.setattr(desktop.Path, "home", lambda: tmp_path)
    assert desktop.home_dir() == tmp_path / "PurffleShorts"
    (tmp_path / "Documents").mkdir()
    assert desktop.home_dir() == tmp_path / "Documents" / "PurffleShorts"


def test_free_port_moves_off_a_taken_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        taken = s.getsockname()[1]
        port = desktop.free_port(taken)
    assert port != taken and port > 0


def test_second_launch_opens_the_running_studio(monkeypatch, tmp_path):
    monkeypatch.setenv("PURFFLE_HOME", str(tmp_path))
    opened, started = [], []
    monkeypatch.setattr(desktop, "running_studio", lambda port: True)
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    monkeypatch.setattr("purffle_shorts.cli.main", lambda argv: started.append(argv) or 0)
    assert desktop.main([]) == 0
    assert opened == ["http://127.0.0.1:8765/"] and not started
    assert (tmp_path / "logs").is_dir()


def test_first_launch_starts_studio_in_the_home_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("PURFFLE_HOME", str(tmp_path))
    started = []
    monkeypatch.setattr(desktop, "running_studio", lambda port: False)
    monkeypatch.setattr(desktop, "free_port", lambda preferred=8765: 9123)
    monkeypatch.setattr("purffle_shorts.cli.main", lambda argv: started.append(argv) or 0)
    assert desktop.main([]) == 0
    assert started == [["studio", "--port", "9123"]]


def test_running_studio_is_false_when_nothing_answers():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert desktop.running_studio(port) is False


def test_mac_home_is_application_support(monkeypatch, tmp_path):
    monkeypatch.delenv("PURFFLE_HOME", raising=False)
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.Path, "home", lambda: tmp_path)
    assert desktop.home_dir() == tmp_path / "Library" / "Application Support" / "PurffleShorts"


def test_free_port_skips_a_port_something_answers_on():
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        s.listen()
        taken = s.getsockname()[1]
        assert desktop.free_port(taken) != taken


def _windows_env(tmp_path):
    src = tmp_path / "from-windows"
    src.mkdir()
    (src / ".env").write_bytes(
        b"# mine\r\nLLM_PROVIDER=gemini\r\nLLM_MODEL=gemini-2.5-flash\r\nGEMINI_API_KEY=AIza-test-0123456789\r\n"
        b"OPENAI_API_KEY=\r\nGOOGLE_CLIENT_SECRETS_FILE=C:\\Users\\me\\credentials.json\r\n"
        b"MEDIA_DIR=D:\\clips\\media\r\nOUTPUT_DIR=output_videos\r\nDATA_DIR=data\r\n")
    (src / "credentials.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()
    return src / ".env", home


def test_import_env_from_windows_without_keychain(monkeypatch, tmp_path):
    from purffle_shorts import envfile, macapp
    monkeypatch.delenv("PURFFLE_KEYCHAIN", raising=False)
    monkeypatch.setattr(macapp.Path, "home", lambda: tmp_path)
    src, home = _windows_env(tmp_path)
    report = macapp.import_env(src, home, interactive=False)
    env = envfile.read(home / ".env")
    assert env["LLM_PROVIDER"] == "gemini" and env["LLM_MODEL"] == "gemini-2.5-flash"
    assert env["GEMINI_API_KEY"] == "AIza-test-0123456789"  # no Keychain: kept in the private file
    assert "OPENAI_API_KEY" not in env and "MEDIA_DIR" not in env and env["DATA_DIR"] == "data"
    assert env["GOOGLE_CLIENT_SECRETS_FILE"] == "credentials.json" and (home / "credentials.json").exists()
    assert env["OUTPUT_DIR"] == str(tmp_path / "Movies" / "PurffleShorts")
    assert "AIza-test" not in "\n".join(report) and any("MEDIA_DIR" in r for r in report)
    if desktop.sys.platform != "win32":
        assert (home / ".env").stat().st_mode & 0o777 == 0o600


def test_import_env_puts_secrets_in_the_keychain(monkeypatch, tmp_path):
    from purffle_shorts import envfile, macapp
    chain = {}
    monkeypatch.setattr(macapp, "keychain_enabled", lambda: True)
    monkeypatch.setattr(macapp, "keychain_set", lambda k, v: chain.__setitem__(k, v) or True)
    monkeypatch.setattr(macapp, "keychain_names", lambda: list(chain))
    monkeypatch.setattr(macapp, "keychain_delete", chain.pop)
    src, home = _windows_env(tmp_path)
    report = macapp.import_env(src, home, interactive=False)
    assert chain == {"GEMINI_API_KEY": "AIza-test-0123456789"}
    assert "AIza-test" not in (home / ".env").read_text() and "GEMINI_API_KEY" not in envfile.read(home / ".env")
    assert report[1] == "In the Keychain: GEMINI_API_KEY"


def test_windows_paths_and_secret_names():
    from purffle_shorts import envfile, macapp
    assert macapp.is_windows_path(r"C:\Users\me\a.json") and macapp.is_windows_path(r"\\nas\share")
    assert not macapp.is_windows_path("/Users/me/a.json") and not macapp.is_windows_path("media")
    assert all(map(envfile.is_secret, ["GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_AUTH_TOKEN", "STUDIO_PASSWORD",
                                       "NOTIFY_WEBHOOK", "INSTAGRAM_ACCESS_TOKEN"]))
    assert not any(map(envfile.is_secret, ["LLM_MODEL", "YT_TOKEN_FILE", "UPLOAD_POST_USER", "GOOGLE_CLIENT_SECRETS_FILE"]))


def test_log_lines_never_show_a_key(monkeypatch):
    import logging
    monkeypatch.setattr(logging.Formatter, "format", logging.Formatter.format)  # undone after the test
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-secret-value-123")
    desktop.scrub_logs()
    rec = logging.LogRecord("purffle", logging.ERROR, "", 0, "call failed: %s", ("key=AIza-secret-value-123",), None)
    assert logging.Formatter("%(message)s").format(rec) == "call failed: key=***"

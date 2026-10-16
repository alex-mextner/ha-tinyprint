"""bridge/install.sh in a throw-away HOME: idempotent, never overwrites or prints secrets."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "bridge" / "install.sh"
UNIT = ROOT / "bridge" / "tinyprint-bridge.service"
PRINTER = "4C:2A:EE:D3:3E:C2"
ADAPTER = "00:1A:7D:DA:71:13"
SHARED = {"protocol.py", "transport.py", "att.py", "bridge_wire.py"}


def run(home, *args, check=True):
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    result = subprocess.run(
        ["bash", str(INSTALLER), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        timeout=60,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def home(tmp_path):
    return tmp_path


def paths(home):
    config = home / ".config" / "tinyprint-bridge"
    data = home / ".local" / "share" / "tinyprint-bridge"
    return config, data


COMMON = ["--no-systemd", "--no-venv", "--printer-mac", PRINTER, "--adapter-mac", ADAPTER]


def test_fresh_install_layout_modes_and_secrets(home):
    result = run(home, *COMMON)
    config, data = paths(home)
    env_file, token_file = config / "bridge.env", config / "token"
    assert mode(config) == 0o700 and mode(data) == 0o700
    assert mode(env_file) == 0o600 and mode(token_file) == 0o600
    token = token_file.read_text().strip()
    assert len(token) >= 32
    assert token not in result.stdout + result.stderr
    text = env_file.read_text()
    assert f"TINYPRINT_BRIDGE_PRINTER_MAC={PRINTER}" in text
    assert f"TINYPRINT_BRIDGE_ADAPTER_MAC={ADAPTER}" in text
    assert f"TINYPRINT_BRIDGE_TOKEN_FILE={token_file}" in text
    assert "TINYPRINT_BRIDGE_BIND=127.0.0.1" in text and "TINYPRINT_BRIDGE_PORT=8765" in text
    assert token not in text
    assert str(token_file) in result.stdout  # tells the user where to read it
    app = data / "app"
    assert (app / "bridge" / "tinyprint_bridge" / "__main__.py").is_file()
    shared = {p.name for p in (app / "custom_components" / "tinyprint").iterdir()}
    assert shared == SHARED  # never the HA-importing package files
    assert not list(app.rglob("__pycache__"))


def test_installed_layout_can_import_the_shared_modules(home):
    run(home, *COMMON)
    _config, data = paths(home)
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); import tinyprint_bridge._shared as s; "
        "print(s.protocol.WIDTH, s.att.AttClient.__name__, s.bridge_wire.API_PREFIX)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(data / "app" / "bridge")],
        env={},
        capture_output=True,
        text=True,
        cwd=home,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["384", "AttClient", "/v1"]


def test_rerun_is_idempotent_and_never_overwrites_secrets(home):
    run(home, *COMMON)
    config, data = paths(home)
    token_before = (config / "token").read_text()
    env_file = config / "bridge.env"
    env_file.write_text(env_file.read_text() + "# local edit\n")
    marker = data / "app" / "bridge" / "stale.txt"
    marker.write_text("stale")
    second = run(home, "--no-systemd", "--no-venv")  # no MAC flags needed on re-run
    assert (config / "token").read_text() == token_before
    assert "# local edit" in env_file.read_text()
    assert not marker.exists()  # code tree is replaced, not accumulated
    assert token_before.strip() not in second.stdout + second.stderr
    assert mode(config / "token") == 0o600 and mode(env_file) == 0o600
    third = run(home, *COMMON, "--bind", "100.64.0.9", "--port", "9999")
    assert "100.64.0.9" not in env_file.read_text()  # existing env is never rewritten
    assert "kept" in (third.stdout + third.stderr).lower()


def test_existing_token_is_preserved_on_first_install(home):
    config, _data = paths(home)
    config.mkdir(parents=True, mode=0o700)
    (config / "token").write_text("pre-existing-token-" + "z" * 30)
    (config / "token").chmod(0o600)
    run(home, *COMMON)
    assert (config / "token").read_text() == "pre-existing-token-" + "z" * 30


def test_bind_and_port_flags_land_in_the_new_env_file(home):
    run(home, *COMMON, "--bind", "100.64.0.9", "--port", "9100")
    config, _data = paths(home)
    text = (config / "bridge.env").read_text()
    assert "TINYPRINT_BRIDGE_BIND=100.64.0.9" in text and "TINYPRINT_BRIDGE_PORT=9100" in text


def test_custom_prefix_and_config_dir(home):
    prefix, config = home / "opt" / "bridge", home / "etc-bridge"
    run(home, *COMMON, "--prefix", str(prefix), "--config-dir", str(config))
    assert (prefix / "app" / "bridge" / "tinyprint_bridge" / "__init__.py").is_file()
    assert (config / "token").is_file() and mode(config) == 0o700


def test_missing_macs_on_first_install_fails_actionably_and_creates_nothing(home):
    result = run(home, "--no-systemd", "--no-venv", check=False)
    assert result.returncode == 2
    assert "--printer-mac" in result.stderr
    config, data = paths(home)
    assert not config.exists() and not data.exists()


@pytest.mark.parametrize("bad", ["4C:2A:EE", "zz:2a:ee:d3:3e:c2", "4C-2A-EE-D3-3E-C2", "$(id)"])
def test_bad_mac_is_rejected(home, bad):
    result = run(
        home, "--no-systemd", "--no-venv", "--printer-mac", bad, "--adapter-mac", ADAPTER,
        check=False,
    )  # fmt: skip
    assert result.returncode == 2 and "MAC" in result.stderr
    assert not paths(home)[0].exists()


@pytest.mark.parametrize(
    "flag,value", [("--port", "0"), ("--port", "70000"), ("--port", "x"), ("--bind", "")]
)
def test_bad_bind_or_port_is_rejected(home, flag, value):
    result = run(home, *COMMON, flag, value, check=False)
    assert result.returncode == 2


def test_unknown_flag_is_rejected(home):
    result = run(home, "--no-systemd", "--disable-safety", check=False)
    assert result.returncode == 2


def test_systemd_unit_is_hardened_and_uses_the_installed_layout():
    text = UNIT.read_text()
    assert "ExecStart=%h/.local/share/tinyprint-bridge/venv/bin/python -m tinyprint_bridge" in text
    assert "WorkingDirectory=%h/.local/share/tinyprint-bridge/app/bridge" in text
    assert "Environment=PYTHONPATH=%h/.local/share/tinyprint-bridge/app/bridge" in text
    assert "EnvironmentFile=%h/.config/tinyprint-bridge/bridge.env" in text
    for line in (
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_BLUETOOTH",
        "Restart=on-failure",
    ):
        assert line in text
    assert "AmbientCapabilities" not in text
    assert "sudo" not in text.lower()


def test_systemd_install_rewrites_custom_paths_and_never_uses_sudo(home):
    fakebin = home / "fakebin"
    fakebin.mkdir()
    log = home / "systemctl.log"
    script = fakebin / "systemctl"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    script.chmod(0o755)
    prefix, config = home / "data", home / "cfg"
    python = prefix / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    env = {"PATH": f"{fakebin}:{os.environ['PATH']}", "HOME": str(home)}
    result = subprocess.run(
        ["bash", str(INSTALLER), "--no-venv", "--printer-mac", PRINTER, "--adapter-mac", ADAPTER]
        + ["--prefix", str(prefix), "--config-dir", str(config)],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    unit = (home / ".config" / "systemd" / "user" / "tinyprint-bridge.service").read_text()
    assert f"ExecStart={prefix}/venv/bin/python -m tinyprint_bridge" in unit
    assert f"EnvironmentFile={config}/bridge.env" in unit and "%h" not in unit
    calls = log.read_text().splitlines()
    assert calls == [
        "--user daemon-reload",
        "--user enable tinyprint-bridge.service",
        "--user restart tinyprint-bridge.service",
    ]


def copy_checkout(destination):
    """A minimal checkout at an arbitrary location (the installer must not care where)."""
    import shutil

    shutil.copytree(
        ROOT / "bridge", destination / "bridge", ignore=shutil.ignore_patterns("__pycache__")
    )
    shared = destination / "custom_components" / "tinyprint"
    shared.mkdir(parents=True)
    for name in SHARED:
        shutil.copy(ROOT / "custom_components" / "tinyprint" / name, shared / name)


def run_from(checkout, home, *args, check=True):
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    result = subprocess.run(
        ["bash", str(checkout / "bridge" / "install.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        timeout=60,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_installed_copy_survives_removal_of_the_checkout_it_came_from(home):
    import shutil

    checkout = home / "some where" / "ha tinyprint worktree"
    copy_checkout(checkout)
    run_from(checkout, home, *COMMON)
    shutil.rmtree(checkout.parent)
    _config, data = paths(home)
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); import tinyprint_bridge._shared as s; "
        "print(s.protocol.WIDTH, s.att.AttClient.__name__)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(data / "app" / "bridge")],
        env={},
        capture_output=True,
        text=True,
        cwd=home,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["384", "AttClient"]


def test_checkout_inside_the_prefix_is_not_touched_by_the_code_refresh(home):
    _config, data = paths(home)
    checkout = data / "source"
    copy_checkout(checkout)
    run_from(checkout, home, *COMMON)
    run_from(checkout, home, "--no-systemd", "--no-venv")
    assert (checkout / "bridge" / "install.sh").is_file()
    assert (data / "app" / "bridge" / "tinyprint_bridge" / "__init__.py").is_file()


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--prefix", ""),
        ("--prefix", "relative/dir"),
        ("--prefix", "/tmp/a b"),
        ("--prefix", "/tmp/a|b"),
        ("--prefix", "/tmp/a&b"),
        ("--prefix", "/tmp/a%hb"),
        ("--config-dir", ""),
        ("--config-dir", "relative"),
        ("--config-dir", "/tmp/a'b"),
    ],
)
def test_unsafe_prefix_or_config_dir_is_rejected_before_anything_is_created(home, flag, value):
    result = run(home, *COMMON, flag, value, check=False)
    assert result.returncode == 2 and flag in result.stderr
    assert not paths(home)[0].exists() and not paths(home)[1].exists()


def test_empty_token_file_left_by_a_failed_run_is_regenerated(home):
    config, _data = paths(home)
    config.mkdir(parents=True, mode=0o700)
    (config / "token").write_text("")
    (config / "token").chmod(0o600)
    run(home, *COMMON)
    assert len((config / "token").read_text().strip()) >= 32


def test_old_python_is_refused_before_anything_is_created(home):
    fakebin = home / "fakebin"
    fakebin.mkdir()
    script = fakebin / "python3"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    env = {"PATH": f"{fakebin}:{os.environ['PATH']}", "HOME": str(home)}
    result = subprocess.run(
        ["bash", str(INSTALLER), *COMMON],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        timeout=60,
    )
    assert result.returncode == 2 and "3.14" in result.stderr
    assert not paths(home)[0].exists() and not paths(home)[1].exists()


def test_rerun_does_not_need_the_network_when_aiohttp_is_already_installed(home):
    _config, data = paths(home)
    python = data / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    log = home / "venv-calls.log"
    python.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\ncase "$1" in -c) exit 0;; *) exit 1;; esac\n'
    )
    python.chmod(0o755)
    run(home, "--no-systemd", "--printer-mac", PRINTER, "--adapter-mac", ADAPTER)
    assert "pip" not in log.read_text()


def test_unrelated_app_directory_in_a_broad_prefix_is_never_deleted(home):
    sentinel = home / "app" / "bridge" / "precious.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("mine")
    other = home / "app" / "custom_components" / "other.txt"
    other.parent.mkdir(parents=True)
    other.write_text("mine")
    result = run(home, *COMMON, "--prefix", str(home), check=False)
    assert result.returncode == 2 and "app" in result.stderr
    assert sentinel.read_text() == "mine" and other.read_text() == "mine"
    assert not paths(home)[0].exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--printer-mac", "00:00:00:00:00:00", "--adapter-mac", ADAPTER],
        ["--printer-mac", PRINTER, "--adapter-mac", "00:00:00:00:00:00"],
        ["--printer-mac", PRINTER, "--adapter-mac", ADAPTER, "--bind", "999.999.999.999"],
        ["--printer-mac", PRINTER, "--adapter-mac", ADAPTER, "--bind", "::::"],
        ["--printer-mac", PRINTER, "--adapter-mac", ADAPTER, "--bind", "1.2.3"],
    ],
)
def test_values_the_bridge_would_refuse_at_startup_are_refused_by_the_installer(home, args):
    result = run(home, "--no-systemd", "--no-venv", *args, check=False)
    assert result.returncode == 2
    assert not paths(home)[0].exists() and not paths(home)[1].exists()


@pytest.mark.parametrize("which", ["prefix", "config"])
def test_symlinked_prefix_or_config_dir_is_refused(home, which):
    real = home / "real"
    (real / "app" / "bridge").mkdir(parents=True)
    (real / "app" / "bridge" / "precious.txt").write_text("mine")
    (real / "app" / ".tinyprint-bridge-app").write_text("")
    alias = home / "alias"
    alias.symlink_to(real)
    flag = "--prefix" if which == "prefix" else "--config-dir"
    result = run(home, *COMMON, flag, str(alias), check=False)
    assert result.returncode == 2 and "symlink" in result.stderr
    assert (real / "app" / "bridge" / "precious.txt").read_text() == "mine"


def test_symlinked_app_directory_is_refused_and_its_target_untouched(home):
    _config, data = paths(home)
    victim = home / "victim"
    (victim / "bridge").mkdir(parents=True)
    (victim / "bridge" / "precious.txt").write_text("mine")
    (victim / ".tinyprint-bridge-app").write_text("")  # even a forged marker must not help
    data.mkdir(parents=True)
    (data / "app").symlink_to(victim)
    result = run(home, *COMMON, check=False)
    assert result.returncode == 2 and "symlink" in result.stderr
    assert (victim / "bridge" / "precious.txt").read_text() == "mine"


def test_default_directory_failure_names_home_not_a_flag_the_user_never_passed(home):
    spaced = home / "jane doe"
    spaced.mkdir()
    result = run(spaced, *COMMON, check=False)
    assert result.returncode == 2 and "HOME" in result.stderr


def test_no_venv_with_systemd_requires_an_existing_venv(home):
    result = run(home, "--no-venv", "--printer-mac", PRINTER, "--adapter-mac", ADAPTER, check=False)
    assert result.returncode == 2 and "--no-venv" in result.stderr
    assert not paths(home)[0].exists() and not paths(home)[1].exists()
    assert not (home / ".config" / "systemd").exists()

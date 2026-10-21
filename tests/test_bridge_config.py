"""Bridge configuration refuses unsafe or incomplete setups and never echoes secrets."""

import os

import pytest
from tinyprint_bridge import __main__ as entry
from tinyprint_bridge import config

TOKEN = "Zq7-" + "k" * 44
PRINTER = "4c:2a:ee:d3:3e:c2"
ADAPTER = "00:1A:7D:DA:71:13"


def env(tmp_path, **overrides):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    values = {
        "TINYPRINT_BRIDGE_PRINTER_MAC": PRINTER,
        "TINYPRINT_BRIDGE_ADAPTER_MAC": ADAPTER,
        "TINYPRINT_BRIDGE_TOKEN_FILE": str(token_file),
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def test_valid_config_with_defaults(tmp_path):
    loaded = config.load_config(env(tmp_path))
    assert loaded.printer_mac == "4C:2A:EE:D3:3E:C2" and loaded.adapter_mac == ADAPTER
    assert loaded.token == TOKEN
    assert (loaded.bind, loaded.port) == ("127.0.0.1", 8765) and not loaded.is_wildcard
    assert TOKEN not in repr(loaded)


def test_tail_rows_default_override_and_validation(tmp_path):
    assert config.load_config(env(tmp_path)).tail_rows == 96
    assert config.load_config(env(tmp_path, TINYPRINT_BRIDGE_TAIL_FEED_LINES="0")).tail_rows == 0
    assert (
        config.load_config(env(tmp_path, TINYPRINT_BRIDGE_TAIL_FEED_LINES="512")).tail_rows == 512
    )
    for bad in ("-1", "513", "many", "1.5", ""):
        with pytest.raises(config.ConfigError, match="TINYPRINT_BRIDGE_TAIL_FEED_LINES"):
            config.load_config(env(tmp_path, TINYPRINT_BRIDGE_TAIL_FEED_LINES=bad))


def test_environment_overrides_and_cli_flags_win(tmp_path):
    values = env(tmp_path, TINYPRINT_BRIDGE_BIND="100.64.0.7", TINYPRINT_BRIDGE_PORT="9000")
    loaded = config.load_config(values)
    assert (loaded.bind, loaded.port) == ("100.64.0.7", 9000)
    loaded = config.load_config(values, bind="127.0.0.2", port=9100)
    assert (loaded.bind, loaded.port) == ("127.0.0.2", 9100)


def test_token_from_environment_is_allowed(tmp_path):
    values = env(tmp_path, TINYPRINT_BRIDGE_TOKEN_FILE=None, TINYPRINT_BRIDGE_TOKEN=TOKEN)
    assert config.load_config(values).token == TOKEN


@pytest.mark.parametrize("bad", ["", "short", "A" * 31, "A" * 31 + " ", "A" * 40 + "é"])
def test_weak_or_odd_tokens_refuse_to_start(tmp_path, bad):
    values = env(tmp_path, TINYPRINT_BRIDGE_TOKEN_FILE=None, TINYPRINT_BRIDGE_TOKEN=bad)
    with pytest.raises(config.ConfigError) as caught:
        config.load_config(values)
    assert bad not in str(caught.value) or bad == ""


def test_missing_token_and_ambiguous_token(tmp_path):
    with pytest.raises(config.ConfigError, match="required"):
        config.load_config(env(tmp_path, TINYPRINT_BRIDGE_TOKEN_FILE=None))
    with pytest.raises(config.ConfigError, match="only one"):
        config.load_config(env(tmp_path, TINYPRINT_BRIDGE_TOKEN=TOKEN))


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o777])
def test_group_or_world_accessible_token_file_is_refused(tmp_path, mode):
    values = env(tmp_path)
    os.chmod(values["TINYPRINT_BRIDGE_TOKEN_FILE"], mode)
    with pytest.raises(config.ConfigError, match="accessible"):
        config.load_config(values)


def test_token_file_must_be_regular_and_not_a_symlink(tmp_path):
    values = env(tmp_path)
    real = values["TINYPRINT_BRIDGE_TOKEN_FILE"]
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(config.ConfigError):
        config.load_config(env(tmp_path, TINYPRINT_BRIDGE_TOKEN_FILE=str(link)))
    with pytest.raises(config.ConfigError, match="regular"):
        config.load_config(env(tmp_path, TINYPRINT_BRIDGE_TOKEN_FILE=str(tmp_path)))
    with pytest.raises(config.ConfigError):
        config.load_config(env(tmp_path, TINYPRINT_BRIDGE_TOKEN_FILE=str(tmp_path / "missing")))


def test_oversized_token_file_is_refused(tmp_path):
    big = tmp_path / "big"
    big.write_text("a" * 10000)
    big.chmod(0o600)
    with pytest.raises(config.ConfigError, match="too large"):
        config.load_config(env(tmp_path, TINYPRINT_BRIDGE_TOKEN_FILE=str(big)))


@pytest.mark.parametrize("name", ["TINYPRINT_BRIDGE_PRINTER_MAC", "TINYPRINT_BRIDGE_ADAPTER_MAC"])
@pytest.mark.parametrize(
    "bad",
    [None, "", "4C:2A:EE:D3:3E", "4C-2A-EE-D3-3E-C2", "ZZ:2A:EE:D3:3E:C2", "00:00:00:00:00:00"],
)
def test_macs_must_be_explicit_and_valid(tmp_path, name, bad):
    with pytest.raises(config.ConfigError, match=name):
        config.load_config(env(tmp_path, **{name: bad}))


@pytest.mark.parametrize("bad", ["localhost", "example.com", "", "999.1.1.1", "1.2.3"])
def test_bind_must_be_an_ip_address(tmp_path, bad):
    with pytest.raises(config.ConfigError, match="IP address"):
        config.load_config(env(tmp_path, TINYPRINT_BRIDGE_BIND=bad))


@pytest.mark.parametrize("bad", ["0", "65536", "abc", "-1", ""])
def test_port_range(tmp_path, bad):
    with pytest.raises(config.ConfigError, match="Port"):
        config.load_config(env(tmp_path, TINYPRINT_BRIDGE_PORT=bad))


def test_wildcard_addresses_are_flagged(tmp_path):
    assert config.load_config(env(tmp_path, TINYPRINT_BRIDGE_BIND="0.0.0.0")).is_wildcard
    assert config.load_config(env(tmp_path, TINYPRINT_BRIDGE_BIND="::")).is_wildcard


def test_main_exits_2_without_printing_secrets(tmp_path, monkeypatch, capsys):
    for key in list(os.environ):
        if key.startswith("TINYPRINT_BRIDGE_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("TINYPRINT_BRIDGE_PRINTER_MAC", PRINTER)
    monkeypatch.setenv("TINYPRINT_BRIDGE_ADAPTER_MAC", ADAPTER)
    monkeypatch.setenv("TINYPRINT_BRIDGE_TOKEN", "short-secret-token")
    assert entry.main([]) == 2
    captured = capsys.readouterr()
    assert "configuration error" in captured.err
    assert "short-secret-token" not in captured.out + captured.err

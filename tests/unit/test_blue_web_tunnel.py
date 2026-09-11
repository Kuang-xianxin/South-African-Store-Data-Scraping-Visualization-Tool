from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/ha"))
    return importlib.import_module("install_blue_web_tunnel_keys")


@pytest.mark.parametrize("node,port", [("main", 18505), ("laptop", 18506)])
def test_key_can_only_listen_on_its_blue_loopback_port(keys, node, port):
    line = keys.restricted_key(f"ssh-ed25519 AAAA takealot-blue-web-{node}", node)
    assert f'permitlisten="127.0.0.1:{port}"' in line
    assert line.count('permitlisten=') == 1
    assert 'permitopen="127.0.0.1:1"' in line
    assert line.startswith('restrict,port-forwarding,')
    assert 'command="/usr/local/libexec/takealot-relay-hold"' in line


@pytest.mark.parametrize("key", [
    'ssh-ed25519 AAAA takealot-blue-web-laptop',
    'ssh-ed25519 AAAA takealot-blue-web-main\nssh-ed25519 BBBB injected',
    'command="sh" ssh-ed25519 AAAA takealot-blue-web-main',
])
def test_wrong_node_or_extra_authorization_is_rejected(keys, key):
    with pytest.raises(ValueError):
        keys.restricted_key(key, 'main')

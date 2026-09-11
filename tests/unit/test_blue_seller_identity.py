import base64
import importlib
from pathlib import Path
import sys
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
arbiter = importlib.import_module("blue_arbiter")
keys = importlib.import_module("register_blue_seller_key")
runtime = importlib.import_module("blue_seller_runtime")


@pytest.mark.parametrize("action", ["report", "status", "acquire", "renew", "fenced", ""])
def test_seller_only_ssh_key_cannot_access_database_arbitration(action):
    with pytest.raises(ValueError, match="cannot-access-database"):
        arbiter.validate_scope({"action": action}, seller_only=True)
    arbiter.validate_scope({"action": action}, seller_only=False)


def record():
    payload = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32
    return {"version": 1, "cluster": "takealot-blue-3307-v1", "node": "main", "sid": "S-1-5-18",
            "scope": "seller-only", "public_key": "ssh-ed25519 " + base64.b64encode(payload).decode()}


def test_registration_forces_verified_node_and_forbids_forwarding():
    line = keys.authorized_line(record())
    assert line.startswith('restrict,command="/usr/bin/python3 /usr/local/libexec/takealot-blue-arbiter main --seller-only" ')
    arbiter.validate_scope({"action": "seller.status"}, seller_only=True)


@pytest.mark.parametrize("field,value", [("node", 'main --db'), ("scope", "all"),
    ("sid", 'S-1-5-18\ncommand="sh"'), ("public_key", "ssh-ed25519 broken")])
def test_key_registration_rejects_scope_or_command_injection(field, value):
    data = record()
    data[field] = value
    with pytest.raises(ValueError):
        keys.authorized_line(data)


def test_each_task_identity_uses_its_own_seller_key_not_database_key(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "current_sid", lambda: "S-1-5-18")
    key = tmp_path / "secrets/seller-api/S-1-5-18/id_ed25519"
    key.parent.mkdir(parents=True)
    key.write_text("test fixture")
    original = ["ssh.exe", "-o", "StrictHostKeyChecking=yes", "-i", "system-database-key", "pinned-host"]
    monkeypatch.setitem(sys.modules, "blue_arbiter_observer", types.SimpleNamespace(ssh_command=lambda _: list(original)))
    command = runtime.seller_ssh_command({})
    assert command[command.index("-i") + 1] == str(key)
    assert original[4] == "system-database-key"
    assert "StrictHostKeyChecking=yes" in command
    monkeypatch.setattr(runtime, "current_sid", lambda: "S-1-5-21-123")
    with pytest.raises(runtime.AdmissionError, match="not installed"):
        runtime.seller_ssh_command({})


def test_sid_is_read_from_actual_process_identity(monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "check_output", lambda *_, **__: b'"HOST\\Mayn","S-1-5-21-123"\r\n')
    assert runtime.current_sid() == "S-1-5-21-123"


def test_real_wire_transport_checks_handshake_and_does_not_replay():
    code = 'import json,sys; print(json.dumps({"cluster":"wrong","status":"hello","protocol":1}),flush=True); sys.stdin.readline()'
    rpc = runtime.SSH([sys.executable, "-u", "-c", code])
    with pytest.raises(runtime.AdmissionError, match="handshake"):
        rpc({"action": "seller.status"})
    assert rpc.process is None


def test_real_wire_transport_roundtrip_and_close():
    code = ('import json,sys; print(json.dumps({"cluster":"takealot-blue-3307-v1","status":"hello","protocol":1}),flush=True); '
            'request=json.loads(sys.stdin.readline()); print(json.dumps({"status":"ok","action":request["action"]}),flush=True)')
    rpc = runtime.SSH([sys.executable, "-u", "-c", code])
    try:
        assert rpc({"action": "seller.status"}) == {"status": "ok", "action": "seller.status"}
    finally:
        rpc.close()
    assert rpc.process is None

"""Append two restricted BLUE web tunnel keys to the existing cloud relay user."""
from pathlib import Path
import re
import shutil
import sys
import time


def restricted_key(public_key: str, node: str) -> str:
    port = {"main": 18505, "laptop": 18506}[node]
    key = public_key.strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+ takealot-blue-web-" + node, key):
        raise ValueError("Unexpected BLUE public key")
    return (f'restrict,port-forwarding,permitopen="127.0.0.1:1",'
            f'permitlisten="127.0.0.1:{port}",'
            'command="/usr/local/libexec/takealot-relay-hold" ' + key)


def main() -> None:
    stage = Path(sys.argv[1]).resolve()
    target = Path('/var/lib/takealot-relay/.ssh/authorized_keys')
    if target.is_symlink() or not target.is_file():
        raise RuntimeError('Existing relay keys required')
    old = target.read_text()
    lines = [restricted_key((stage / f'{node}.pub').read_text(), node) for node in ('main', 'laptop')]
    if 'takealot-blue-web-' in old:
        raise RuntimeError('BLUE web key already installed; inspect before replacement')
    backup = target.with_name(f'authorized_keys.pre-blue-web-{int(time.time())}')
    shutil.copy2(target, backup)
    target.write_text(old.rstrip() + '\n' + '\n'.join(lines) + '\n')
    target.chmod(0o600)
    assert target.read_text().startswith(old.rstrip() + '\n')
    print(f'Added BLUE-only loopback listeners 18505/18506; backup={backup}')


if __name__ == '__main__':
    main()

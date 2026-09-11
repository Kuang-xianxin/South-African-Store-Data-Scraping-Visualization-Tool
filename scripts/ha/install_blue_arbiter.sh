#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# All input files are prepared separately; public keys only, never private keys.
[[ "$(id -u)" == 0 && "$(hostname -s)" == VM-0-12-ubuntu ]] || exit 64
readonly source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly arbiter_user=takealot-blue-arbiter
readonly arbiter_home=/var/lib/takealot-blue-arbiter
readonly policy=/etc/ssh/sshd_config.d/60-takealot-blue-arbiter.conf
readonly executable=/usr/local/libexec/takealot-blue-arbiter

python3 - "$source_dir" <<'PY'
import json, pathlib, re, sys, uuid
root = pathlib.Path(sys.argv[1])
cfg = json.loads((root / 'blue-arbiter.json').read_text())
assert cfg['cluster'] == 'takealot-blue-3307-v1' and cfg['mode'] == 'observe'
assert set(cfg['nodes']) == {'main', 'laptop'}
assert re.fullmatch('[0-9a-f]{64}', cfg['seed_sha256'])
for node in ('main', 'laptop'):
    uuid.UUID(cfg['nodes'][node]['server_uuid'])
    assert re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+ [A-Za-z0-9@._-]+',
                        (root / (node + '.pub')).read_text().strip())
assert cfg['nodes']['main']['server_uuid'] != cfg['nodes']['laptop']['server_uuid']
PY

if [[ -e /etc/takealot-blue-arbiter.json ]]; then
  cmp -s "$source_dir/blue-arbiter.json" /etc/takealot-blue-arbiter.json || {
    echo 'Existing cluster config differs; refusing replacement' >&2; exit 65;
  }
  [[ -f "$arbiter_home/state/ledger.json" ]] || {
    echo 'Existing installation is missing its ledger; refuse reset' >&2; exit 65;
  }
fi
if ! id "$arbiter_user" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$arbiter_home" --shell /bin/sh "$arbiter_user"
fi
# Keep password login disabled. An unlocked random hash permits Ubuntu pubkey authentication.
if passwd -S "$arbiter_user" | awk '{exit $2 == "L" ? 0 : 1}'; then
  random_password="$(openssl rand -base64 48)"
  printf '%s:%s\n' "$arbiter_user" "$random_password" | chpasswd
  unset random_password
fi
install -d -o root -g root -m 0755 "$arbiter_home"
install -d -o root -g root -m 0755 "$arbiter_home/.ssh"
install -d -o "$arbiter_user" -g "$arbiter_user" -m 0700 "$arbiter_home/state"
install -o root -g root -m 0644 "$source_dir/blue-arbiter.json" /etc/takealot-blue-arbiter.json
install -o root -g root -m 0755 "$source_dir/blue_arbiter.py" "$executable"

keys="$(mktemp)"
policy_tmp="$(mktemp)"
policy_was_present=false
[[ ! -e "$policy" ]] || policy_was_present=true
cleanup() {
  exit_code=$?
  if [[ "$exit_code" != 0 && "$policy_was_present" == false && -e "$policy" ]]; then
    mv -- "$policy" "$source_dir/blue-policy-rejected-$(date +%s).conf"
  fi
  rm -f -- "$keys" "$policy_tmp"
}
trap cleanup EXIT
for node in main laptop; do
  printf 'restrict,command="/usr/bin/python3 %s %s" %s\n' "$executable" "$node" "$(tr -d '\r\n' < "$source_dir/$node.pub")" >> "$keys"
done
if [[ -e "$arbiter_home/.ssh/authorized_keys" ]]; then
  cmp -s "$keys" "$arbiter_home/.ssh/authorized_keys" || {
    echo 'Existing node keys differ; refusing rotation' >&2; exit 65;
  }
fi
install -o root -g root -m 0644 "$keys" "$arbiter_home/.ssh/authorized_keys"

if [[ ! -e "$arbiter_home/state/ledger.json" ]]; then
  # Never reconstruct an existing installation with a missing ledger automatically.
  [[ ! -e "$arbiter_home/state/initialized" ]] || { echo 'Missing ledger: fail closed' >&2; exit 65; }
  python3 - "$source_dir" "$arbiter_home/state/ledger.json" <<'PY'
import pathlib, sys
sys.path.insert(0, sys.argv[1])
from blue_arbiter import initial_state, save_state
save_state(pathlib.Path(sys.argv[2]), initial_state())
PY
  chown "$arbiter_user:$arbiter_user" "$arbiter_home/state/ledger.json"
  touch "$arbiter_home/state/initialized"
fi

printf '%s\n' 'AllowUsers takealot-blue-arbiter' 'Match User takealot-blue-arbiter' '    PasswordAuthentication no' '    AuthenticationMethods publickey' '    AllowTcpForwarding no' '    X11Forwarding no' '    PermitTTY no' 'Match all' > "$policy_tmp"
if [[ -e "$policy" ]]; then
  cmp -s "$policy_tmp" "$policy" || { echo 'Existing SSH policy differs' >&2; exit 65; }
fi
install -o root -g root -m 0644 "$policy_tmp" "$policy"
sshd -t
# AllowUsers is additive; verify every previously permitted account remains allowed.
effective="$(sshd -T)"
for account in ubuntu takealot-witness takealot-relay takealot-blue-arbiter; do
  printf '%s\n' "$effective" | grep -qx "allowusers $account"
done
systemctl reload ssh
printf 'installed blue arbiter, observe only; old witness and nginx unchanged\n'

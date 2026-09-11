#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$#" -ne 2 ]]; then
  printf 'usage: %s MAIN_PUBLIC_KEY_BASE64 LAPTOP_PUBLIC_KEY_BASE64\n' "$0" >&2
  exit 64
fi

readonly WITNESS_USER="takealot-witness"
readonly WITNESS_HOME="/var/lib/takealot-witness"
readonly LEASE_TARGET="/usr/local/libexec/takealot-witness-lease"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

decode_public_key() {
  printf '%s' "$1" | base64 --decode
}

validate_public_key() {
  local public_key="$1"
  if [[ ! "${public_key}" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+\ [A-Za-z0-9@._-]+$ ]]; then
    printf 'invalid ed25519 public key payload\n' >&2
    exit 65
  fi
}

main_public_key="$(decode_public_key "$1")"
laptop_public_key="$(decode_public_key "$2")"
validate_public_key "${main_public_key}"
validate_public_key "${laptop_public_key}"

if ! id "${WITNESS_USER}" >/dev/null 2>&1; then
  useradd \
    --system \
    --create-home \
    --home-dir "${WITNESS_HOME}" \
    --shell /bin/bash \
    "${WITNESS_USER}"
fi

# Ubuntu's sshd rejects public-key login for a freshly created account whose
# shadow entry is locked.  Give it a random, undisclosed password hash while
# the server-wide SSH policy keeps password authentication disabled.
if passwd -S "${WITNESS_USER}" | awk '{exit $2 == "L" ? 0 : 1}'; then
  random_password="$(openssl rand -base64 48)"
  printf '%s:%s\n' "${WITNESS_USER}" "${random_password}" | chpasswd
  unset random_password
fi

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 \
  "${SCRIPT_DIR}/takealot_witness_lease.sh" \
  "${LEASE_TARGET}"
install -d -o "${WITNESS_USER}" -g "${WITNESS_USER}" -m 0700 \
  "${WITNESS_HOME}" \
  "${WITNESS_HOME}/.ssh" \
  "${WITNESS_HOME}/state"

authorized_keys_tmp="$(mktemp)"
trap 'rm -f "${authorized_keys_tmp}"' EXIT
printf 'restrict,command="%s desktop-ntrmang" %s\n' \
  "${LEASE_TARGET}" "${main_public_key}" \
  > "${authorized_keys_tmp}"
printf 'restrict,command="%s laptop-2t5mn8eu" %s\n' \
  "${LEASE_TARGET}" "${laptop_public_key}" \
  >> "${authorized_keys_tmp}"
install -o "${WITNESS_USER}" -g "${WITNESS_USER}" -m 0600 \
  "${authorized_keys_tmp}" \
  "${WITNESS_HOME}/.ssh/authorized_keys"

printf 'installed user=%s lease=%s authorized_keys=2\n' \
  "${WITNESS_USER}" "${LEASE_TARGET}"

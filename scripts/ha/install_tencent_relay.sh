#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$#" -ne 2 ]]; then
  printf 'usage: %s MAIN_FORWARD_PUBLIC_KEY_BASE64 LAPTOP_REVERSE_PUBLIC_KEY_BASE64\n' "$0" >&2
  exit 64
fi

if [[ "${EUID}" -ne 0 ]]; then
  printf 'run this installer as root\n' >&2
  exit 77
fi

readonly RELAY_USER="takealot-relay"
readonly RELAY_HOME="/var/lib/takealot-relay"
readonly HOLD_COMMAND="/usr/local/libexec/takealot-relay-hold"
readonly ALLOW_USERS_CONFIG="/etc/ssh/sshd_config.d/99-policy-match-hardening.conf"

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

effective_sshd="$(sshd -T)"
if ! grep -qx 'passwordauthentication no' <<<"${effective_sshd}"; then
  printf 'refusing relay setup while SSH password authentication is enabled\n' >&2
  exit 78
fi

allow_users_backup=""
if ! grep -Eq "^allowusers( .*)? ${RELAY_USER}( |$)|^allowusers ${RELAY_USER}( |$)" \
  <<<"${effective_sshd}"; then
  if [[ ! -f "${ALLOW_USERS_CONFIG}" ]]; then
    printf 'required AllowUsers policy file is missing: %s\n' "${ALLOW_USERS_CONFIG}" >&2
    exit 79
  fi
  allow_users_count="$(grep -Ec '^[[:space:]]*AllowUsers[[:space:]]+' "${ALLOW_USERS_CONFIG}")"
  if [[ "${allow_users_count}" -ne 1 ]]; then
    printf 'expected exactly one AllowUsers directive in %s\n' "${ALLOW_USERS_CONFIG}" >&2
    exit 79
  fi
  allow_users_backup="${ALLOW_USERS_CONFIG}.pre-takealot-relay-$(date +%Y%m%d-%H%M%S)"
  cp -a "${ALLOW_USERS_CONFIG}" "${allow_users_backup}"
  allow_users_tmp="$(mktemp)"
  awk -v user="${RELAY_USER}" '
    /^[[:space:]]*AllowUsers[[:space:]]+/ { print $0 " " user; next }
    { print }
  ' "${ALLOW_USERS_CONFIG}" >"${allow_users_tmp}"
  install \
    -o "$(stat -c %u "${ALLOW_USERS_CONFIG}")" \
    -g "$(stat -c %g "${ALLOW_USERS_CONFIG}")" \
    -m "$(stat -c %a "${ALLOW_USERS_CONFIG}")" \
    "${allow_users_tmp}" \
    "${ALLOW_USERS_CONFIG}"
  rm -f "${allow_users_tmp}"
  if ! sshd -t; then
    cp -a "${allow_users_backup}" "${ALLOW_USERS_CONFIG}"
    printf 'sshd validation failed; restored %s\n' "${allow_users_backup}" >&2
    exit 79
  fi
  systemctl reload ssh
  effective_sshd="$(sshd -T)"
  if ! grep -Eq "^allowusers( .*)? ${RELAY_USER}( |$)|^allowusers ${RELAY_USER}( |$)" \
    <<<"${effective_sshd}"; then
    cp -a "${allow_users_backup}" "${ALLOW_USERS_CONFIG}"
    sshd -t
    systemctl reload ssh
    printf 'effective AllowUsers policy did not include %s; restored backup\n' "${RELAY_USER}" >&2
    exit 79
  fi
fi

if ! id "${RELAY_USER}" >/dev/null 2>&1; then
  useradd \
    --system \
    --create-home \
    --home-dir "${RELAY_HOME}" \
    --shell /bin/bash \
    "${RELAY_USER}"
fi

# Ubuntu rejects public-key login for a system account whose shadow entry is
# locked.  The generated password is never disclosed, while sshd continues to
# reject password authentication globally.
if passwd -S "${RELAY_USER}" | awk '{exit $2 == "L" ? 0 : 1}'; then
  random_password="$(openssl rand -base64 48)"
  printf '%s:%s\n' "${RELAY_USER}" "${random_password}" | chpasswd
  unset random_password
fi

install -d -o root -g root -m 0755 /usr/local/libexec
hold_tmp="$(mktemp)"
keys_tmp="$(mktemp)"
trap 'rm -f "${hold_tmp}" "${keys_tmp}"' EXIT

cat >"${hold_tmp}" <<'EOF'
#!/bin/sh
set -eu
if [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
  exit 126
fi
trap 'exit 0' HUP INT TERM
while :; do
  sleep 3600
done
EOF
install -o root -g root -m 0755 "${hold_tmp}" "${HOLD_COMMAND}"

install -d -o "${RELAY_USER}" -g "${RELAY_USER}" -m 0700 \
  "${RELAY_HOME}" \
  "${RELAY_HOME}/.ssh"

# Main may only open local forwards to the three relay endpoints.  Its dummy
# port-1 permitlisten prevents useful reverse forwarding with this key.
printf '%s %s\n' \
  "restrict,port-forwarding,permitlisten=\"127.0.0.1:1\",permitopen=\"127.0.0.1:22022\",permitopen=\"127.0.0.1:23306\",permitopen=\"127.0.0.1:27897\",command=\"${HOLD_COMMAND}\"" \
  "${main_public_key}" \
  >"${keys_tmp}"

# Laptop may only create the three reverse listeners on server loopback.  Its
# dummy port-1 permitopen prevents useful local forwarding with this key.
printf '%s %s\n' \
  "restrict,port-forwarding,permitopen=\"127.0.0.1:1\",permitlisten=\"127.0.0.1:22022\",permitlisten=\"127.0.0.1:23306\",permitlisten=\"127.0.0.1:27897\",command=\"${HOLD_COMMAND}\"" \
  "${laptop_public_key}" \
  >>"${keys_tmp}"

install -o "${RELAY_USER}" -g "${RELAY_USER}" -m 0600 \
  "${keys_tmp}" \
  "${RELAY_HOME}/.ssh/authorized_keys"

sshd -t
printf 'installed user=%s authorized_keys=2 listeners=127.0.0.1:{22022,23306,27897}\n' \
  "${RELAY_USER}"
if [[ -n "${allow_users_backup}" ]]; then
  printf 'allow_users_backup=%s\n' "${allow_users_backup}"
fi

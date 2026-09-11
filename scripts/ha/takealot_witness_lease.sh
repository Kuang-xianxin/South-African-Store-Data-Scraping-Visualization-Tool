#!/usr/bin/env bash
set -Eeuo pipefail

readonly PROTOCOL=1
readonly NODE_ID="${1:-}"
readonly STATE_DIR="/var/lib/takealot-witness/state"
readonly LOCK_FILE="${STATE_DIR}/leader.lock"
readonly EPOCH_FILE="${STATE_DIR}/epoch"
readonly LEADER_FILE="${STATE_DIR}/leader.json"
readonly LEASE_TIMEOUT_SECONDS=8

case "${NODE_ID}" in
  desktop-ntrmang|laptop-2t5mn8eu) ;;
  *)
    printf '{"protocol":%d,"status":"error","detail":"invalid node identity"}\n' "${PROTOCOL}"
    exit 64
    ;;
esac

umask 077
mkdir -p "${STATE_DIR}"
exec 9>"${LOCK_FILE}"

if ! flock -n 9; then
  leader="unknown"
  epoch=""
  if [[ -s "${LEADER_FILE}" ]]; then
    leader="$(sed -n 's/.*"node":"\([a-z0-9-]*\)".*/\1/p' "${LEADER_FILE}" | head -n 1)"
    epoch="$(sed -n 's/.*"epoch":\([0-9][0-9]*\).*/\1/p' "${LEADER_FILE}" | head -n 1)"
  fi
  [[ -n "${leader}" ]] || leader="unknown"
  if [[ "${epoch}" =~ ^[1-9][0-9]*$ ]]; then
    printf '{"protocol":%d,"status":"busy","leader":"%s","epoch":%s}\n' \
      "${PROTOCOL}" "${leader}" "${epoch}"
  else
    printf '{"protocol":%d,"status":"busy","leader":"%s"}\n' \
      "${PROTOCOL}" "${leader}"
  fi
  exit 75
fi

previous_epoch=0
if [[ -s "${EPOCH_FILE}" ]]; then
  read -r previous_epoch < "${EPOCH_FILE}" || previous_epoch=0
fi
[[ "${previous_epoch}" =~ ^[0-9]+$ ]] || previous_epoch=0
epoch=$((previous_epoch + 1))
printf '%d\n' "${epoch}" > "${EPOCH_FILE}.tmp"
mv -f "${EPOCH_FILE}.tmp" "${EPOCH_FILE}"

server_unix_ms="$(date +%s%3N)"
printf '{"protocol":%d,"status":"granted","node":"%s","epoch":%d,"server_unix_ms":%s,"lease_timeout_seconds":%d}\n' \
  "${PROTOCOL}" "${NODE_ID}" "${epoch}" "${server_unix_ms}" "${LEASE_TIMEOUT_SECONDS}" \
  > "${LEADER_FILE}.tmp"
mv -f "${LEADER_FILE}.tmp" "${LEADER_FILE}"
cat "${LEADER_FILE}"

cleanup() {
  rm -f "${LEADER_FILE}"
}
trap cleanup EXIT HUP INT TERM

while IFS= read -r -t "${LEASE_TIMEOUT_SECONDS}" request; do
  request="${request%$'\r'}"
  if [[ "${request}" != "PING" ]]; then
    printf '{"protocol":%d,"status":"error","detail":"expected PING"}\n' "${PROTOCOL}"
    exit 65
  fi
  server_unix_ms="$(date +%s%3N)"
  printf '{"protocol":%d,"status":"alive","node":"%s","epoch":%d,"server_unix_ms":%s,"lease_timeout_seconds":%d}\n' \
    "${PROTOCOL}" "${NODE_ID}" "${epoch}" "${server_unix_ms}" "${LEASE_TIMEOUT_SECONDS}"
done

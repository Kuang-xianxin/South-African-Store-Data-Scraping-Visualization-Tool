#!/usr/bin/env bash
# One-time migration: only the verified legacy public blue vhost may be replaced.
set -euo pipefail
candidate=${1:?Expected the uploaded new blue nginx configuration}
target=/etc/nginx/conf.d/takealot-erp-blue-public.conf
expected=b35fabdaf121fd97537000203566791059fd89d44f0e5390e346662b06e7a481
[[ $(id -u) == 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
[[ -f "$candidate" && ! -L "$candidate" && ! -L "$target" ]]
[[ $(sha256sum "$target" | cut -d' ' -f1) == "$expected" ]] || {
  echo 'Existing blue vhost changed; refusing overwrite' >&2; exit 1;
}
grep -q 'server 100.122.102.37:8503 ' "$candidate"
! grep -Eq ':850[12]|listen .*:443 ' "$candidate"
curl --noproxy '*' --connect-timeout 5 --max-time 15 -fsS \
  http://100.122.102.37:8503/api/health | grep -q 'blue-stage-laptop'
green_files=(/etc/nginx/conf.d/takealot-erp-tailnet.conf /etc/nginx/conf.d/takealot-erp-zz-public.conf)
green_before=$(sha256sum "${green_files[@]}")
archive=$(mktemp -d /var/backups/takealot-blue-entry.XXXXXX)
chmod 700 "$archive"
cp -p -- "$target" "$archive/legacy-blue.conf"
rollback() {
  cp -p -- "$archive/legacy-blue.conf" "$target"
  if nginx -t; then systemctl reload nginx; fi
  echo "Migration failed; restored legacy blue vhost from $archive" >&2
}
trap rollback ERR
install -m 644 -- "$candidate" "$target"
nginx -t
systemctl reload nginx
# A successful reload is asynchronous. Validate the response identity, not just
# curl's transport exit code, while old workers finish accepting connections.
healthy=false
for attempt in {1..10}; do
  if body=$(curl --noproxy '*' --connect-timeout 5 --max-time 10 -fsS \
    https://119.91.117.232:8443/api/health) && grep -q 'blue-stage-laptop' <<< "$body"; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  echo 'New public blue identity did not become ready after reload' >&2
  false
fi
[[ $(sha256sum "${green_files[@]}") == "$green_before" ]]
curl --noproxy '*' --connect-timeout 5 --max-time 15 -fsS \
  https://119.91.117.232/api/health | grep -q 'takealot-erp'
trap - ERR
echo "Public blue migrated to laptop 8503; rollback=$archive/legacy-blue.conf"
sha256sum "$target" "${green_files[@]}"

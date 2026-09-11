#!/usr/bin/env bash
# Explicit BLUE listener preparation/activation; green remains separate.
set -euo pipefail
stage=/home/ubuntu/takealot-blue-routing-20260909
target=/etc/nginx/conf.d/takealot-erp-blue-public.conf
[[ $(id -u) == 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
[[ ! -L "$target" && ! -L "$stage" ]]
case "${1:-}" in
prepare)
 install -d -m 755 /opt/takealot-blue-routing
 for file in blue_web_router.py blue_web_routes.py blue_web_ownership.py blue_seller_refresh_policy.py; do
  install -m 644 "$stage/$file" "/opt/takealot-blue-routing/$file"
 done
 install -m 600 "$stage/web-routing-cloud.json" /etc/takealot-blue-routing.json
 python3 - <<'PY'
import json,re
from pathlib import Path
cfg=json.loads(Path('/etc/takealot-blue-routing.json').read_text())
assert re.fullmatch('[a-f0-9]{64}', cfg['secret'])
assert re.fullmatch('[a-f0-9]{64}', cfg['frontend_sha256'])
path=Path('/etc/takealot-blue-routing-proxy.conf')
assert not path.is_symlink()
path.write_text('proxy_set_header X-Blue-Routing-Key "'+cfg['secret']+'";\n')
path.chmod(0o600)
PY
 install -m 644 "$stage/takealot_blue_routing.service" /etc/systemd/system/takealot-blue-routing.service
 systemd-analyze verify /etc/systemd/system/takealot-blue-routing.service
 systemctl daemon-reload
 systemctl enable --now takealot-blue-routing.service
 systemctl is-active takealot-blue-routing.service
 ;;
activate)
 expected=e57fb11fa06595747569204a8d7b540c2ce9c46d8f7c860776f89a22b2b6bd4f
 [[ $(sha256sum "$target" | cut -d' ' -f1) == "$expected" ]] || { echo 'BLUE ingress baseline changed'; exit 1; }
 python3 - <<'PY'
import json,urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:18504/status',timeout=3) as r: state=json.load(r)
cfg=json.load(open('/etc/takealot-blue-routing.json'))
for node in ('main','laptop'):
 s=state['samples'][node]
 assert s['age_seconds']<12 and s['frontend_sha256']==cfg['frontend_sha256'],node
print('Both BLUE nodes have fresh verified capacity')
PY
 green=(/etc/nginx/conf.d/takealot-erp-tailnet.conf /etc/nginx/conf.d/takealot-erp-zz-public.conf)
 before=$(sha256sum "${green[@]}")
 backup=$(mktemp -d /var/backups/takealot-blue-routing.XXXXXX)
 chmod 700 "$backup"
 cp -p "$target" "$backup/blue-public.conf"
 rollback(){ cp -p "$backup/blue-public.conf" "$target"; nginx -t && systemctl reload nginx; }
 trap rollback ERR
 install -m 644 "$stage/takealot_erp_blue_public_nginx.conf" "$target"
 nginx -t
 systemctl reload nginx
 [[ $(sha256sum "${green[@]}") == "$before" ]]
 trap - ERR
 printf 'BLUE ingress activated; rollback=%s\n' "$backup"
 ;;
*) echo 'Use prepare or activate' >&2; exit 1;;
esac

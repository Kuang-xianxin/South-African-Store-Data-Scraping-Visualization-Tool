#!/usr/bin/env bash
set -euo pipefail
stage=/home/ubuntu/takealot-blue-ownership-20260909
target=/etc/nginx/conf.d/takealot-erp-blue-public.conf
[[ $(id -u) == 0 ]]
[[ $(sha256sum "$target" | cut -d' ' -f1) == 1d4be3780bdad4ede80e5332f8d2d67a142a61eee3ecafe39a621031deb8e772 ]]
python3 - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:18504/status',timeout=3) as r: state=json.load(r)
for node in ('main','laptop'):
    sample=state['samples'][node]
    assert sample['routing_version']==2 and sample['age_seconds']<12,node
    assert sample['frontend_sha256']=='770f10b3fdbc6c557703d05f50e260a0bfd961ee5b2a7825577243eb511ebf6e',node
    assert not any(w['busy'] for w in sample['workflows'].values()), 'Preserve active BLUE workflows'
print('Both BLUE inventories verified and no local workflows active')
PY
backup=$(mktemp -d /var/backups/takealot-blue-ownership.XXXXXX)
chmod 700 "$backup"
cp -p "$target" "$backup/nginx.conf"
cp -p /opt/takealot-blue-routing/blue_web_router.py "$backup/router.py"
cp -p /etc/systemd/system/takealot-blue-routing.service "$backup/service"
green=(/etc/nginx/conf.d/takealot-erp-tailnet.conf /etc/nginx/conf.d/takealot-erp-zz-public.conf)
before=$(sha256sum "${green[@]}")
rollback(){
 cp -p "$backup/nginx.conf" "$target"
 cp -p "$backup/router.py" /opt/takealot-blue-routing/blue_web_router.py
 cp -p "$backup/service" /etc/systemd/system/takealot-blue-routing.service
 systemctl daemon-reload
 systemctl restart takealot-blue-routing.service
 nginx -t && systemctl reload nginx
}
trap rollback ERR
for file in blue_web_router.py blue_web_routes.py blue_web_ownership.py blue_seller_refresh_policy.py; do
 install -m 644 "$stage/$file" "/opt/takealot-blue-routing/$file"
done
python3 -m py_compile /opt/takealot-blue-routing/blue_web_router.py /opt/takealot-blue-routing/blue_web_routes.py /opt/takealot-blue-routing/blue_web_ownership.py /opt/takealot-blue-routing/blue_seller_refresh_policy.py
install -m 644 "$stage/takealot_blue_routing.service" /etc/systemd/system/takealot-blue-routing.service
systemctl daemon-reload
systemctl restart takealot-blue-routing.service
for step in $(seq 1 20); do
 if curl -fsS --max-time 2 http://127.0.0.1:18504/status | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("routing_version")==2 and set(s["samples"])=={"main","laptop"}'; then break; fi
 sleep 1
done
systemctl is-active takealot-blue-routing.service
curl -fsS --max-time 3 http://127.0.0.1:18504/status | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("routing_version")==2 and set(s["samples"])=={"main","laptop"}; assert all(v.get("routing_version")==2 and v["age_seconds"]<12 for v in s["samples"].values())'
install -m 644 "$stage/takealot_erp_blue_public_nginx.conf" "$target"
nginx -t
systemctl reload nginx
[[ $(sha256sum "${green[@]}") == "$before" ]]
trap - ERR
printf 'BLUE ownership enabled; rollback=%s\n' "$backup"

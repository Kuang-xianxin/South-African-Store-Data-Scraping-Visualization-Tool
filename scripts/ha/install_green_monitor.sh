#!/bin/sh
set -eu
test "$(id -u)" = 0
test "$(hostname)" = VM-0-12-ubuntu
if ! id takealot-monitor >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin takealot-monitor
fi
install -d -o root -g root -m 755 /opt/takealot-green-monitor
install -o root -g root -m 644 /tmp/green-monitor-stage/green_public_monitor.py /opt/takealot-green-monitor/green_public_monitor.py
install -o root -g root -m 644 /tmp/green-monitor-stage/takealot_green_monitor.service /etc/systemd/system/takealot-green-monitor.service
install -o root -g root -m 644 /tmp/green-monitor-stage/takealot_green_monitor.timer /etc/systemd/system/takealot-green-monitor.timer
install -o root -g root -m 644 /tmp/green-monitor-stage/takealot_green_monitor.logrotate /etc/logrotate.d/takealot-green-monitor
if test ! -e /var/log/nginx/takealot-green-perf.jsonl; then
    install -o www-data -g adm -m 640 /dev/null /var/log/nginx/takealot-green-perf.jsonl
fi
install -o root -g root -m 644 /tmp/green-monitor-stage/takealot_green_performance_nginx.conf /etc/nginx/conf.d/00-takealot-green-performance.conf
python3 -m py_compile /opt/takealot-green-monitor/green_public_monitor.py
systemd-analyze verify /etc/systemd/system/takealot-green-monitor.service /etc/systemd/system/takealot-green-monitor.timer
systemctl daemon-reload
systemctl enable --now takealot-green-monitor.timer
systemctl start takealot-green-monitor.service
systemctl is-active takealot-green-monitor.timer
cat /var/lib/takealot-green-monitor/status.json

#!/bin/sh
set -eu
test "$(id -u)" = 0
test "$(hostname)" = VM-0-12-ubuntu
config=/etc/ssh/sshd_config.d/61-takealot-green.conf
test ! -e "$config"
test -s /tmp/takealot-green-service.pub
test -s /tmp/takealot-green-sshd.conf
grep -Eq '^ssh-ed25519 [A-Za-z0-9+/=]+ ' /tmp/takealot-green-service.pub
if ! id takealot-green >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin takealot-green
fi
install -d -o root -g root -m 755 /home/takealot-green/.ssh
{ printf 'restrict,port-forwarding,permitlisten="127.0.0.1:18503" '; cat /tmp/takealot-green-service.pub; } > /home/takealot-green/.ssh/authorized_keys
chown root:root /home/takealot-green/.ssh/authorized_keys
chmod 644 /home/takealot-green/.ssh/authorized_keys
install -o root -g root -m 644 /tmp/takealot-green-sshd.conf "$config"
if ! /usr/sbin/sshd -t; then
    rm -f "$config"
    exit 1
fi
systemctl reload ssh
/usr/sbin/sshd -T -C user=takealot-green,host=takealot-witness,addr=14.155.151.192 |
    grep -E '^(allowtcpforwarding|allowstreamlocalforwarding|permitlisten|permitopen|gatewayports|maxsessions|passwordauthentication|permittty|clientalive)'

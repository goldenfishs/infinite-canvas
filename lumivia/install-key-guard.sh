#!/usr/bin/env bash
# Establish durable protection before the app is allowed to mint managed keys.
# A canvas release rollback must retain the guard for previously issued credentials.
set -euo pipefail
rel=$1
site=/etc/nginx/sites-available
state=/opt/lumivia-key-guard
unit=/etc/systemd/system/lumivia-key-guard.service
backup=$rel/guard.before
install -d -m700 "$backup" /etc/lumivia-canvas
install -d -m755 "$state"
# Guard every configured HTTP ingress to the loopback-only Sub2 listener.
python3 - <<'PY'
from pathlib import Path
import subprocess
ports=subprocess.check_output(['docker','port','sub2api','8080/tcp'],text=True).splitlines()
if ports!=['127.0.0.1:8080']: raise SystemExit('Refusing: Sub2 must publish only 127.0.0.1:8080')
config=subprocess.check_output(['nginx','-T'],stderr=subprocess.DEVNULL,text=True)
import re
sites=[n for n,s in re.findall(r'# configuration file ([^\n]+):\n(.*?)(?=\n# configuration file |\Z)',config,re.S) if 'proxy_pass http://127.0.0.1:8080' in s]
if {Path(n).name for n in sites} != {'sub2api','sub2api-cn-api'}: raise SystemExit('Unexpected Sub2 ingress; inspect before deploying')
PY
for n in sub2api sub2api-cn-api; do cp "$site/$n" "$backup/$n"; done
for f in key_guard.py managed_keys.py; do if [[ -f "$state/$f" ]]; then cp "$state/$f" "$backup/$f"; fi; done
if [[ -f "$unit" ]]; then cp "$unit" "$backup/unit"; fi
undo_guard() {
    trap - ERR
    for n in sub2api sub2api-cn-api; do cp "$backup/$n" "$site/$n"; done
    for f in key_guard.py managed_keys.py; do if [[ -f "$backup/$f" ]]; then cp "$backup/$f" "$state/$f"; fi; done
    if [[ -f "$backup/unit" ]]; then cp "$backup/unit" "$unit"; systemctl daemon-reload; systemctl restart lumivia-key-guard; fi
    nginx -t && systemctl reload nginx
}
trap undo_guard ERR
if [[ ! -e /etc/lumivia-canvas/key-seed ]]; then
    (umask 077; tmpseed=$(mktemp /etc/lumivia-canvas/.seed.XXXXXX); head -c 32 /dev/urandom > "$tmpseed"; ln "$tmpseed" /etc/lumivia-canvas/key-seed; rm -f "$tmpseed")
fi
test "$(wc -c < /etc/lumivia-canvas/key-seed)" -eq 32
install -m644 "$rel/lumivia/"{key_guard.py,managed_keys.py} "$state/"
cat > "$unit" <<'UNIT'
[Unit]
Description=Lumivia managed key protection
After=network.target
[Service]
ExecStart=/usr/bin/python3 /opt/lumivia-key-guard/key_guard.py
Restart=on-failure
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
PrivateTmp=yes
ProtectHome=yes
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now lumivia-key-guard
systemctl restart lumivia-key-guard
curl -fsS --retry 8 --retry-delay 1 --retry-connrefused http://127.0.0.1:3032/healthz >/dev/null
python3 - "$site" <<'PY'
import sys,re
from pathlib import Path
# Nginx matches normalized paths; the guard only forwards confirmed, canonical API routes.
block='''    # lumivia-key-guard begin
    location ~ ^/api/v1/(?:keys(?:/|$)|usage(?:/|$)|auth/me/?$|user/profile/?$) {
        access_log off;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_pass http://127.0.0.1:3032;
    }
    # lumivia-key-guard end
'''
for name in ('sub2api','sub2api-cn-api'):
    f=Path(sys.argv[1])/name
    s=re.sub(r'    # lumivia-key-guard begin.*?    # lumivia-key-guard end\n','',f.read_text(),flags=re.S)
    m=re.search(r'    location / \{\s*proxy_pass http://127\.0\.0\.1:8080;',s)
    if not m: raise SystemExit('Missing Sub2 site proxy '+name)
    f.write_text(s[:m.start()]+block+s[m.start():])
PY
nginx -t
systemctl reload nginx
# An anonymous list must reach the guard & preserve Sub2 authentication before minting is enabled.
for host in api.lumivia.cn cn-api.lumivia.cn; do
    ready=0
    for attempt in {1..15}; do
        code=$(curl -sS --resolve "$host:443:127.0.0.1" -D "$backup/probe.headers" -o /dev/null -w '%{http_code}' "https://$host/api/v1/keys")
        if [[ "$code" = 401 ]] && grep -iq '^X-Lumivia-Key-Guard: 1' "$backup/probe.headers"; then ready=1; break; fi
        sleep 1
    done
    test "$ready" = 1
done
trap - ERR

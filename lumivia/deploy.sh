#!/usr/bin/env bash
# On the HK host: pass the uploaded tarball (dist/ and lumivia/).
set -euo pipefail
base=/opt/infinite-canvas
rev=$(date -u +%Y%m%d%H%M%S)
rel=$base/releases/$rev
install -d -m755 "$rel"
tar xzf "$1" -C "$rel"
test -s "$rel/dist/index.html"
python3 -c 'import aiohttp'
old=$(docker inspect -f '{{.Image}}' lumivia-canvas)
docker cp lumivia-canvas:/usr/share/nginx/html/config.js "$rel/dist/config.js"
chmod -R a+rX "$rel/dist"
port=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
name=lumivia-canvas-$rev
docker run -d --name "$name" --restart unless-stopped --security-opt no-new-privileges --entrypoint nginx -v "$rel/dist:/usr/share/nginx/html:ro" -p "127.0.0.1:$port:3000" "$old" -g 'daemon off;' >/dev/null
curl -fsS --retry 6 --retry-connrefused --retry-delay 1 "http://127.0.0.1:$port/canvas" -o /dev/null || { docker rm -f "$name" >/dev/null; exit 1; }
bash "$rel/lumivia/install-key-guard.sh" "$rel" || { docker rm -f "$name" >/dev/null; exit 1; }
site=/etc/nginx/sites-available
cp "$site/lumivia-canvas" "$rel/canvas.before"; cp "$site/sub2api" "$rel/sub2.before"
bridge=/opt/lumivia-canvas-bridge
backup=$rel/bridge.before
install -d -m700 "$backup"
unit=/etc/systemd/system/lumivia-canvas.service
if [[ -f "$unit" ]]; then cp "$unit" "$backup/service"; fi
for file in bridge.py cloud_storage.py managed_keys.py consent.html entry.html; do
    if [[ -f "$bridge/$file" ]]; then cp "$bridge/$file" "$backup/"; fi
done
rollback() {
    trap - ERR
    cp "$rel/canvas.before" "$site/lumivia-canvas"
    cp "$rel/sub2.before" "$site/sub2api"
    for file in bridge.py cloud_storage.py managed_keys.py consent.html entry.html; do
        if [[ -f "$backup/$file" ]]; then install -m644 "$backup/$file" "$bridge/"; else rm -f "$bridge/$file"; fi
    done
    if [[ -f "$backup/service" ]]; then
        cp "$backup/service" "$unit"
        systemctl daemon-reload
        systemctl restart lumivia-canvas.service
    else
        systemctl disable --now lumivia-canvas.service
        rm -f "$unit"
        systemctl daemon-reload
    fi
    nginx -t && systemctl reload nginx
    docker rm -f "$name" >/dev/null
}
trap rollback ERR
# The original installation directory is root-only; use a traversable dedicated service tree.
install -d -m755 /opt/lumivia-canvas-bridge
install -m644 "$rel"/lumivia/{bridge.py,cloud_storage.py,managed_keys.py,consent.html,entry.html} /opt/lumivia-canvas-bridge/
sed 's|/opt/infinite-canvas/lumivia/|/opt/lumivia-canvas-bridge/|' "$rel/lumivia/lumivia-canvas.service" > "$unit"
systemctl daemon-reload
systemctl enable --now lumivia-canvas.service
systemctl restart lumivia-canvas.service
curl -fsS --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:3031/canvas-entry | grep -q 乐米画布
python3 - "$port" <<'PY'
from pathlib import Path
import re,sys
p=Path('/etc/nginx/sites-available')
def change(name,rules,up=None):
    f=p/name; src=f.read_text(); pos=src.index('listen 443'); a,b=src[:pos],src[pos:]
    b=re.sub(r'    # canvas-bridge begin.*?    # canvas-bridge end\n','',b,flags=re.S)
    at=b.index('    location / {'); b=b[:at]+'    # canvas-bridge begin\n'+rules+'    # canvas-bridge end\n'+b[at:]
    if up: b=re.sub(r'(location / \{\s*proxy_pass http://127.0.0.1:)\d+',lambda m:m[1]+up,b,count=1)
    f.write_text(a+b)
def location(path,extra=''):
    return '    location '+path+' {\n        access_log off;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_pass http://127.0.0.1:3031;\n'+extra+'    }\n'
change('sub2api',location('= /canvas-entry')+location('= /canvas-connect/')+location('= /canvas-connect/approve'))
change('lumivia-canvas',location('^~ /lumivia/','        client_max_body_size 64m;\n        proxy_read_timeout 610s;\n        proxy_request_buffering off;\n        proxy_buffering off;\n        proxy_http_version 1.1;\n'),sys.argv[1])
PY
nginx -t && systemctl reload nginx
ready=0
for attempt in {1..12}; do
  if curl -fsS --resolve canvas.lumivia.cn:443:127.0.0.1 https://canvas.lumivia.cn/canvas | grep -q 乐米画布 && curl -fsS --resolve api.lumivia.cn:443:127.0.0.1 https://api.lumivia.cn/canvas-connect/ | grep -q '乐米画布账号同步' && curl -fsS --resolve api.lumivia.cn:443:127.0.0.1 https://api.lumivia.cn/canvas-entry | grep -q 'iframe'; then ready=1; break; fi
  sleep 1
done
test "$ready" -eq 1
curl -fsS http://127.0.0.1:8080/health >/dev/null
trap - ERR
printf 'release=%s container=%s\n' "$rel" "$name"

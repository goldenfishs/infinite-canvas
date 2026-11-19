#!/usr/bin/env bash
# On the HK host: pass the uploaded tarball (dist/ and lumivia/).
set -Eeuo pipefail
base=/opt/lumivia-canvas-releases
rev=$(date -u +%Y%m%d%H%M%S)
rel=$base/$rev
install -d -m755 "$rel"
tar xzf "$1" -C "$rel"
test -s "$rel/dist/index.html"
[[ -f /etc/lumivia-canvas/key-seed ]]
curl -fsS --max-time 5 http://127.0.0.1:8080/health >/dev/null
python3 -m venv "$rel/venv"
"$rel/venv/bin/python" -m pip install --disable-pip-version-check --quiet -r "$rel/lumivia/requirements.txt"
PYTHONPATH="$rel/lumivia" "$rel/venv/bin/python" -c 'import bridge, cryptography, ijson, PIL, qcloud_cos'
old=$(docker inspect -f '{{.Image}}' lumivia-canvas)
docker cp lumivia-canvas:/usr/share/nginx/html/config.js "$rel/dist/config.js"
chmod -R a+rX "$rel/dist"
port=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
name=lumivia-canvas-$rev
docker run -d --name "$name" --restart unless-stopped --security-opt no-new-privileges --entrypoint nginx -v "$rel/dist:/usr/share/nginx/html:ro" -p "127.0.0.1:$port:3000" "$old" -g 'daemon off;' >/dev/null
curl -fsS --retry 6 --retry-connrefused --retry-delay 1 "http://127.0.0.1:$port/canvas" -o /dev/null || { docker rm -f "$name" >/dev/null; exit 1; }
site=/etc/nginx/sites-available
if ! curl -fsS --max-time 5 http://127.0.0.1:3032/healthz >/dev/null ||
    ! cmp -s "$rel/lumivia/key_guard.py" /opt/lumivia-key-guard/key_guard.py ||
    ! grep -q '127.0.0.1:3032' "$site/sub2api" || ! grep -q '127.0.0.1:3032' "$site/sub2api-cn-api"; then
    bash "$rel/lumivia/install-key-guard.sh" "$rel" || { docker rm -f "$name" >/dev/null; exit 1; }
fi
cp "$site/lumivia-canvas" "$rel/canvas.before"; cp "$site/sub2api" "$rel/sub2.before"
backup=$rel/rollback
install -d -m700 "$backup"
unit=/etc/systemd/system/lumivia-canvas.service
cp "$unit" "$backup/service"
install -m600 /etc/lumivia-canvas/key-seed "$backup/key-seed"
rollback() {
    trap - ERR
    bash "$rel/lumivia/rollback.sh" "$rel" "$name"
    exit 1
}
trap rollback ERR
# Consistent snapshot with the canvas writer stopped. Keep it root-only and separate from the live data.
systemctl stop lumivia-canvas.service
tar -cf "$backup/state.tar" -C "$(readlink -f /var/lib/lumivia-canvas)" .
sed "s|@CANVAS_RELEASE@|$rel|g" "$rel/lumivia/lumivia-canvas.service" > "$unit"
systemctl daemon-reload
systemctl start lumivia-canvas.service
curl -fsS --retry 10 --retry-connrefused --retry-delay 1 --max-time 8 http://127.0.0.1:3031/lumivia/health
python3 - "$port" <<'PY'
from pathlib import Path
import re,sys
p=Path('/etc/nginx/sites-available')
def change(name,rules,up=None):
    f=p/name; src=f.read_text(); pos=src.index('listen 443'); a,b=src[:pos],src[pos:]
    b=re.sub(r'    # canvas-bridge begin.*?    # canvas-bridge end\n','',b,flags=re.S)
    at=b.index('    location / {'); b=b[:at]+'    # canvas-bridge begin\n'+rules+'    # canvas-bridge end\n'+b[at:]
    if up: b=re.sub(r'(location / \{\s*proxy_pass http://127.0.0.1:)\d+',lambda m:m[1]+up,b,count=1)
    if name == 'lumivia-canvas':
        a=re.sub(r'# canvas-asset-cache begin.*?# canvas-asset-cache end\n','',a,flags=re.S)
        a=asset_cache+a
    f.write_text(a+b)
def location(path,extra=''):
    return '    location '+path+' {\n        access_log off;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_pass http://127.0.0.1:3031;\n'+extra+'    }\n'
change('sub2api',location('= /canvas-entry')+location('= /canvas-connect/')+location('= /canvas-connect/approve'))
asset_cache = """# canvas-asset-cache begin
map $upstream_http_content_type $lumivia_canvas_asset_cache {
    default "";
    ~^application/javascript "public, max-age=31536000, immutable";
    ~^text/javascript        "public, max-age=31536000, immutable";
    ~^text/css               "public, max-age=31536000, immutable";
}
# canvas-asset-cache end
"""
assets = f"""    location ^~ /assets/ {{
        gzip on;
        gzip_vary on;
        gzip_proxied any;
        gzip_min_length 1024;
        gzip_types application/javascript text/javascript text/css application/json image/svg+xml;
        add_header Cache-Control $lumivia_canvas_asset_cache;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_pass http://127.0.0.1:{sys.argv[1]};
    }}
"""
change('lumivia-canvas',location('^~ /lumivia/','        client_max_body_size 65m;\n        proxy_read_timeout 610s;\n        proxy_request_buffering off;\n        proxy_buffering off;\n        proxy_http_version 1.1;\n')+assets,sys.argv[1])
PY
nginx -t
systemctl reload nginx
ready=0
for attempt in {1..12}; do
  if curl -fsS --resolve canvas.lumivia.cn:443:127.0.0.1 https://canvas.lumivia.cn/canvas | grep -q 乐米画布 && curl -fsS --resolve api.lumivia.cn:443:127.0.0.1 https://api.lumivia.cn/canvas-connect/ | grep -q '乐米画布账号同步' && curl -fsS --resolve api.lumivia.cn:443:127.0.0.1 https://api.lumivia.cn/canvas-entry | grep -q 'iframe'; then ready=1; break; fi
  sleep 1
done
test "$ready" -eq 1
curl -fsS http://127.0.0.1:8080/health >/dev/null
curl -fsS --resolve canvas.lumivia.cn:443:127.0.0.1 https://canvas.lumivia.cn/lumivia/health >/dev/null
trap - ERR
printf 'release=%s container=%s\n' "$rel" "$name"

#!/usr/bin/env bash
# Restore the previous application and routes. The live data may contain new writes, so keep it in place.
set -euo pipefail
rel=$1
container=$2
site=/etc/nginx/sites-available
cp "$rel/canvas.before" "$site/lumivia-canvas"
cp "$rel/sub2.before" "$site/sub2api"
cp "$rel/rollback/service" /etc/systemd/system/lumivia-canvas.service
systemctl daemon-reload
systemctl restart lumivia-canvas.service
nginx -t
systemctl reload nginx
docker stop "$container" >/dev/null

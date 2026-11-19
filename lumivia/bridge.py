"""Single-worker canvas gateway. Sub2 credentials never leave this process for the canvas client."""
import asyncio
import hashlib
import os
import re
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web

MAIN = 'https://api.lumivia.cn'
CANVAS = 'https://canvas.lumivia.cn'
UP = os.getenv('SUB2_URL', 'http://127.0.0.1:8080')
GROUP = int(os.getenv('IMAGE_GROUP_ID', '88'))
KEY_NAME = 'Lumivia Canvas · ' + str(GROUP)
grants = {}
IMAGE_ACTIONS = ('generations', 'edits')
routes = web.RouteTableDef()


def failure(status, message):
    return web.json_response({'error': {'message': message}}, status=status)


def digest(s):
    return hashlib.sha256(s.encode()).hexdigest()


@web.middleware
async def protect(req, handler):
    if req.method == 'POST':
        origin = MAIN if req.path == '/canvas-connect/approve' else CANVAS
        if req.headers.get('Origin') != origin:
            return failure(403, '来源校验失败')
    try:
        resp = await handler(req)
    except (web.HTTPUnauthorized, web.HTTPForbidden) as e:
        resp = failure(e.status, e.text)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        resp = failure(502, '主站暂时无法响应，请重试')
    resp.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})
    return resp


@routes.get('/canvas-entry')
async def entry(req):
    # Sub2 can append auth and navigation parameters to custom-menu URLs.
    # Strip the query before rendering any document or requesting other resources.
    if req.query_string:
        return web.Response(status=303, headers={'Location': '/canvas-entry'})
    html = Path(__file__).with_name('entry.html').read_text()
    return web.Response(text=html, content_type='text/html', headers={
        'Content-Security-Policy': f"default-src 'none'; style-src 'unsafe-inline'; frame-src {CANVAS}; frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
    })


@routes.get('/canvas-connect/')
async def consent(req):
    nonce = secrets.token_urlsafe(24)
    html = Path(__file__).with_name('consent.html').read_text().replace('SCRIPT_NONCE', nonce)
    return web.Response(text=html, content_type='text/html', headers={
        'Content-Security-Policy': f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors {MAIN} {CANVAS}; base-uri 'none'; form-action 'none'",
    })


@routes.post('/canvas-connect/approve')
async def approve(req):
    data = await req.json()
    if not isinstance(data, dict): return failure(400, '账号同步请求格式错误')
    action = data.get('action')
    challenge = data.get('challenge')
    auth = req.headers.get('Authorization', '')
    if action not in ('account', *IMAGE_ACTIONS) or not auth.startswith('Bearer '):
        return failure(400, '账号同步请求格式错误')
    if action != 'account' and (not isinstance(challenge, str) or not re.fullmatch('[a-f0-9]{64}', challenge) or type(data.get('account_id')) is not int):
        return failure(400, '生图授权请求格式错误')
    http = req.app['http']
    headers = {'Authorization': auth, 'User-Agent': req.headers.get('User-Agent', ''), 'X-Forwarded-For': req.headers.get('X-Real-IP', req.remote or '')}

    async def api(path, payload=None):
        async with http.request('GET' if payload is None else 'POST', UP+'/api/v1/'+path, json=payload, headers=headers, allow_redirects=False) as r:
            if r.status == 401: raise web.HTTPUnauthorized(text='请先登录主站')
            if r.status == 403: raise web.HTTPForbidden(text='主站账号没有权限')
            if r.status != 200: raise ValueError('sub2')
            return (await r.json())['data']

    user = await api('auth/me')
    if action != 'account' and data['account_id'] != user['id']:
        return failure(409, '主站账号已切换，请重新确认后再生成')
    groups, rates = await asyncio.gather(api('groups/available'), api('groups/rates')) if action == 'account' else (await api('groups/available'), {})
    group = next((g for g in groups if g['id'] == GROUP), None)
    if not group: return failure(403, '此账号没有生图分组权限')
    async with req.app['key_lock']:
        from urllib.parse import urlencode
        keys = await api('keys?' + urlencode({'group_id': GROUP, 'search': KEY_NAME, 'page_size': 100}))
        def scoped(k):
            return k.get('name') == KEY_NAME and (k.get('group_ids') or [k.get('group_id')]) == [GROUP] and k.get('status') == 'active'
        key = next((k for k in keys['items'] if scoped(k)), None)
        if not key: key = await api('keys', {'name': KEY_NAME, 'group_id': GROUP})
        if not scoped(key): return failure(403, '画布专用密钥分组不正确')
    secret = key['key']
    if action != 'account':
        grant = secrets.token_urlsafe(32)
        grants[grant] = (time.monotonic() + 20, challenge, action, secret)
        return web.json_response({'grant': grant})
    async with http.get(UP+'/v1/models', headers={'Authorization': 'Bearer '+secret}, allow_redirects=False) as r:
        if r.status != 200: return failure(503, '暂时无法读取生图分组模型')
        models = sorted({m['id'] for m in (await r.json()).get('data', []) if re.fullmatch(r'gpt-image-[\w.-]+', m.get('id',''))})
    if not models: return failure(503, '主站没有返回可用图片模型')
    rate = group['image_rate_multiplier'] if group.get('image_rate_independent') else (rates or {}).get(str(GROUP),group['rate_multiplier'])
    prices = {size: round(group['image_price_'+size]*rate,6) for size in ('1k','2k','4k') if group.get('image_price_'+size) is not None and not group.get('peak_rate_enabled')}
    public = {'id': user['id'], 'email': user['email'], 'models': models, 'prices': prices, 'group': group['name']}
    return web.json_response({'account': public})


@routes.post('/lumivia/v1/images/{action:generations|edits}')
async def images(req):
    action = req.match_info['action']
    grant_id = req.headers.get('X-Lumivia-Grant', '')
    verifier = req.headers.get('X-Lumivia-Verifier', '')
    grant = grants.get(grant_id)
    if not grant or grant[0] <= time.monotonic() or grant[2] != action or not re.fullmatch('[a-f0-9]{64}', verifier) or not secrets.compare_digest(grant[1], digest(verifier)):
        return failure(401, '当前主站账号授权已失效，请重试')
    del grants[grant_id]
    if req.content_type not in ('multipart/form-data','application/json'): return failure(415,'不支持的请求格式')
    async with req.app['http'].post(UP+'/v1/images/'+action,data=req.content.iter_chunked(65536),headers={'Authorization':'Bearer '+grant[3],'Content-Type':req.headers['Content-Type'], 'X-Forwarded-For':req.headers.get('X-Real-IP',req.remote or '')},allow_redirects=False) as r:
        # Only relay JSON. The upstream gateway retains all authorization and billing decisions.
        if r.content_type!='application/json': return failure(502,'生图服务返回了非 JSON 响应')
        out = web.StreamResponse(status=r.status,headers={'Content-Type':'application/json','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        await out.prepare(req)
        async for chunk in r.content.iter_chunked(65536): await out.write(chunk)
        await out.write_eof(); return out


async def lifecycle(app):
    app['http'] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600),cookie_jar=aiohttp.DummyCookieJar())
    app['key_lock'] = asyncio.Lock()
    async def expire():
        while True:
            await asyncio.sleep(60)
            for key, value in list(grants.items()):
                if value[0] <= time.monotonic(): grants.pop(key, None)
    task=asyncio.create_task(expire()); yield
    task.cancel(); await asyncio.gather(task,return_exceptions=True); await app['http'].close()


if __name__ == '__main__':
    app=web.Application(middlewares=[protect]); app.cleanup_ctx.append(lifecycle); app.add_routes(routes)
    web.run_app(app,host='127.0.0.1',port=3031,access_log=None)

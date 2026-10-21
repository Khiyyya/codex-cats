"""Single HTTP+WebSocket ingress backed by a protocol-verified, live WS cache.

The optimization is cross-transport and trigger-agnostic. Images are NEVER deleted.
Native HTTP is the wire-preserving fallback. HTTP->WS translation remains an
experimental compatibility mode, NOT an officially verified Codex integration.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import gzip
import hashlib
import json
import logging
import hmac
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, web, WSMsgType
from multidict import CIMultiDict
from websockets.asyncio.client import connect as WebSocketConnect
from websockets.protocol import State
from yarl import URL

from .integration import parse_proxy
from .protocol import Baseline, baseline_after_success, identity, plan, response_event, terminal_event, strict_json
from .metrics import log as metrics_log

LOG = logging.getLogger('uplink12')
# Headers are strictly split between network/transport and application metadata.
HOP = frozenset({'host','connection','upgrade','keep-alive','proxy-authorization',
                 'proxy-authenticate','te','trailer','transfer-encoding',
                 'sec-websocket-key','sec-websocket-version','sec-websocket-extensions',
                 'sec-websocket-protocol','content-length','content-encoding',
                 'content-type','accept','accept-encoding','x-uplink-token'})
# These scope the upstream identity; hashes are never stored with values in logs.
PRINCIPAL = frozenset({'authorization','cookie','x-openai-actor-authorization',
                       'openai-organization','openai-project','x-openai-organization',
                       'x-openai-project','x-openai-account-id'})
MAX_DECOMPRESSED = 512 * 1024 * 1024
WS_SAFE_PAYLOAD = 12 * 1024 * 1024  # 11.2 MB accepted; 22.4 MB rejected with 1009 on live backend.
PREFLIGHT_BYTES = 256 * 1024
PREFLIGHT_TIMEOUT = 5
HTTP_HOP = frozenset({'connection','keep-alive','proxy-authenticate',
                      'proxy-authorization','te','trailer','transfer-encoding','upgrade'})
NATIVE_HEADER_NAMES = web.ResponseKey('native_header_names',frozenset)
BODY_BOUND_HEADERS = ('Content-Digest','Content-MD5','Digest','Signature','Signature-Input')


class connect(WebSocketConnect):
    """Fixed-origin connection: redirects belong to the original client."""
    def process_redirect(self, exc):
        return exc


class UnsegmentableInput(ValueError):
    pass


class WarmupRejected(ValueError):
    pass


class UpstreamPreflightFailed(ConnectionError):
    """The check failed before this pending payload; earlier batches may exist."""


def connection_tokens(headers):
    values=headers.getall('Connection',[]) if hasattr(headers,'getall') else [headers.get('Connection','')]
    return {x.strip().lower() for value in values for x in value.split(',') if x.strip()}


def headers_for_ws(headers):
    nominated = connection_tokens(headers)
    return [(k,v) for k,v in headers.items() if k.lower() not in HOP | nominated]


def principal_hash(headers):
    values = sorted((k.lower(),v) for k,v in headers.items() if k.lower() in PRINCIPAL)
    if not any(k in ('authorization','cookie','x-openai-actor-authorization') for k,v in values):
        return None
    return hashlib.sha256(json.dumps(values, separators=(',',':')).encode()).hexdigest()


def websocket_handshake_hash(headers):
    # Safety: cache is not shared across requests requiring different upstream
    # handshake headers, even if they have the same principal and thread.
    values = sorted((k.lower(),v) for k,v in headers_for_ws(headers))
    return hashlib.sha256(json.dumps(values, separators=(',',':')).encode()).hexdigest()


def with_request_query(target, query):
    # Incoming query belongs to the request and participates in session scope.
    return target.split('?',1)[0]+'?'+query if query else target


def decoded_json(raw: bytes, encoding: str):
    if len(raw) > MAX_DECOMPRESSED:
        raise ValueError('request_too_large')
    enc = (encoding or 'identity').strip().lower()
    if enc in ('identity',''):
        payload = raw
    elif enc == 'gzip':
        import zlib
        d=zlib.decompressobj(wbits=16+zlib.MAX_WBITS)
        payload = d.decompress(raw, MAX_DECOMPRESSED + 1)
        if d.unconsumed_tail or d.unused_data or not d.eof:
            raise ValueError('incomplete_or_oversized_gzip')
    elif enc == 'zstd':
        try:
            import zstandard
            with zstandard.ZstdDecompressor().stream_reader(raw) as stream:
                payload = stream.read(MAX_DECOMPRESSED + 1)
        except ImportError:
            # A local zstd binary can support offline verification; Windows
            # installer includes the zstandard Python dependency.
            try:
                p=subprocess.run(['zstd','-d','--stdout','--quiet'],input=raw,
                                 capture_output=True,timeout=20,check=True)
                payload=p.stdout
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                raise ValueError('zstd_decode_unavailable') from e
    else:
        raise ValueError('unsupported_encoding')
    if len(payload) > MAX_DECOMPRESSED:
        raise ValueError('request_too_large')
    obj=strict_json(payload)
    if not isinstance(obj,dict):
        raise ValueError('not_json_object')
    return obj


def http_to_ws(body):
    # A basic Responses request with streaming and client-managed persistence.
    # Unknown properties remain intact, never edited or filtered.
    if not (isinstance(body,dict) and body.get('stream') is True
            and body.get('store') is False and 'type' not in body
            and not body.get('previous_response_id')
            and ('background' not in body or body['background'] is False)
            and isinstance(body.get('input'),list)):
        return None
    result=dict(body)
    result.pop('stream', None)
    result.pop('background', None)
    result['type']='response.create'
    return result


def ws_frame(body):
    return json.dumps(body,separators=(',',':'),ensure_ascii=False)


def image_count(items):
    if isinstance(items,list):return sum(image_count(item) for item in items)
    if isinstance(items,dict):
        return int(items.get('type') in ('input_image','image_url'))+sum(image_count(v) for v in items.values())
    return 0


@dataclass
class Upstream:
    key: tuple
    socket: object
    baseline: Baseline | None = None
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sent_bytes: int = 0
    retried_full: bool = False
    warmup_count: int = 0
    downstreams: int = 0

    def live(self):
        return self.socket.state is State.OPEN


class Gateway:
    def __init__(self, *, target_ws: str, target_http: str, token: str,
                 host='127.0.0.1', port=8766, upstream_proxy=None,
                 cache_seconds=90, metrics=None, native_mount=False, shutdown_event=None):
        for url,schemes in ((target_ws,('ws','wss')),(target_http,('http','https'))):
            u=urlsplit(url)
            if u.scheme not in schemes or not u.hostname:
                raise ValueError('invalid upstream URL')
            if u.username or u.password or u.fragment:
                raise ValueError('upstream URL must not embed secrets')
        if upstream_proxy is not None:
            try:
                upstream_proxy=parse_proxy(upstream_proxy)
            except (AttributeError,TypeError,ValueError):
                raise ValueError('invalid upstream proxy; must be a credential-free HTTP(S) URL') from None
        if not token or len(token)<16:
            raise ValueError('local token must be 16+ characters')
        if host not in ('127.0.0.1','::1','localhost'):
            raise ValueError('bind loopback only')
        self.target_ws=target_ws
        self.target_http=target_http
        self.token=token
        self.host=host
        self.port=port
        self.upstream_proxy=upstream_proxy
        self.cache_seconds=cache_seconds
        self.metrics=metrics
        self.native_mount=native_mount
        self.shutdown_event=shutdown_event
        self.requests_seen={'http':0,'websocket':0,'native_other':0}
        self.pool={}
        self.guard=asyncio.Lock()
        self.runner=None
        self.site=None
        self.http=None
        self.reaper=None
        self.preflight_counts={'ws_checks':0,'http_checks':0,'blocked':0}

    def diagnostic(self,phase,error=None,**counts):
        """Fixed fields only: never serialize exception messages or request data."""
        if not self.metrics:return
        record={'time':datetime.now(timezone.utc).isoformat(),'phase':phase,**counts}
        if error is not None:
            record['exception']=type(error).__name__
            record['errno']=getattr(error,'errno',None)
            record['http_status']=getattr(error,'status',None)
            record['ws_close_code']=getattr(getattr(error,'rcvd',None),'code',None)
            causes=[];cause=error.__cause__ or error.__context__
            for _ in range(4):
                if cause is None:break
                causes.append({'type':type(cause).__name__,'errno':getattr(cause,'errno',None)})
                cause=cause.__cause__ or cause.__context__
            record['causes']=causes
        try:
            path=Path(self.metrics).with_name('errors.jsonl')
            with path.open('a',encoding='utf-8') as f:f.write(json.dumps(record)+'\n')
        except OSError:pass

    async def start(self):
        app=web.Application(client_max_size=MAX_DECOMPRESSED,
                            handler_args={'auto_decompress':False})
        async def native_response_headers(request, response):
            original=response.get(NATIVE_HEADER_NAMES)
            if original is not None:
                for name in ('Server','Date','Content-Type'):
                    if name.lower() not in original:
                        response.headers.pop(name,None)
        app.on_response_prepare.append(native_response_headers)
        app.router.add_route('*','/{tail:.*}',self.incoming)
        self.runner=web.AppRunner(app,access_log=None)
        await self.runner.setup()
        self.site=web.TCPSite(self.runner,self.host,self.port)
        await self.site.start()
        self.http=ClientSession(timeout=ClientTimeout(total=None,connect=20),
                                auto_decompress=False,trust_env=False,
                                # This is a transparent native HTTP relay. Do
                                # not negotiate compression the caller cannot
                                # decode (notably the built-in image tool).
                                skip_auto_headers={'Accept','Accept-Encoding','User-Agent','Content-Type'},
                                cookie_jar=DummyCookieJar())
        # aiohttp 3.14.5 exposes this policy only as an internal flag. It is
        # pinned and covered by a peer-drop test: only Codex may retry native
        # requests, including methods aiohttp considers idempotent.
        self.http._retry_connection=False
        self.reaper=asyncio.create_task(self.reap_idle())
        return self

    async def close(self):
        if self.reaper:
            self.reaper.cancel()
            await asyncio.gather(self.reaper,return_exceptions=True)
        for s in list(self.pool.values()):
            await s.socket.close()
        self.pool.clear()
        if self.runner:
            await self.runner.cleanup()
        if self.http:
            await self.http.close()

    async def reap_idle(self):
        while True:
            await asyncio.sleep(min(10,max(.05,self.cache_seconds/2)))
            async with self.guard:
                now=time.monotonic()
                for key,item in list(self.pool.items()):
                    if not item.lock.locked() and item.downstreams==0 and now-item.last_used>self.cache_seconds:
                        self.pool.pop(key,None)
                        await item.socket.close()

    async def exchange(self, session, payload_wire, full_wire, optimized):
        """Only replay an explicitly rejected, gateway-created continuation.

        A timeout, partial response, or caller-owned previous_response_id must
        never trigger a replay: execution may already have started.
        """
        size = lambda x: len(x if isinstance(x, bytes) else x.encode('utf-8'))
        session.sent_bytes = size(payload_wire)
        session.retried_full = False
        session.warmup_count = 0
        if size(payload_wire)>=PREFLIGHT_BYTES:
            await self.preflight_ws(session)
        if size(payload_wire)>WS_SAFE_PAYLOAD:
            payload_wire,extra=await self.warm_full_context(session,payload_wire)
            session.sent_bytes=extra+size(payload_wire)
        await self.send_wire(session.socket,payload_wire)
        first = True
        while True:
            chunk = await asyncio.wait_for(session.socket.recv(), timeout=120)
            event = response_event(chunk)
            if event is None:
                raise ValueError('invalid_ws_response_event')
            error = event.get('error')
            if (first and optimized and event.get('type') == 'error'
                    and isinstance(error, dict)
                    and error.get('code') == 'previous_response_not_found'):
                session.baseline = None
                session.retried_full = True
                replay_wire=full_wire
                extra=0
                if size(full_wire)>WS_SAFE_PAYLOAD:
                    replay_wire,extra=await self.warm_full_context(session,full_wire)
                session.sent_bytes += extra+size(replay_wire)
                if size(replay_wire)>=PREFLIGHT_BYTES:
                    await self.preflight_ws(session)
                await self.send_wire(session.socket,replay_wire)
                optimized = False
                continue
            first = False
            yield chunk, event
            if terminal_event(event):
                return

    @staticmethod
    def warmup_batches(request):
        """Partition at whole input-item boundaries, without editing any item.

        Leave the final batch for actual generation. A single oversized item
        cannot be safely split; callers must use native full HTTP in that case.
        """
        items=request.get('input')
        if not isinstance(items,list) or request.get('previous_response_id'):
            raise UnsegmentableInput('cannot partition caller-owned continuation')
        prototype={**request,'generate':False,'input':[], 'previous_response_id':'r'*256}
        prototype.pop('client_metadata',None)
        overhead=len(ws_frame(prototype).encode())+1024
        batches=[];current=[];total=overhead
        for item in items:
            item_size=len(ws_frame(item).encode())+1
            if item_size+overhead>WS_SAFE_PAYLOAD:
                raise UnsegmentableInput('one input item exceeds safe WS payload')
            if current and total+item_size>WS_SAFE_PAYLOAD:
                batches.append(current);current=[];total=overhead
            current.append(item);total+=item_size
        if current:batches.append(current)
        if len(batches)<2:
            raise UnsegmentableInput('no safe whole-item partition')
        return batches

    async def warm_full_context(self,session,wire):
        request=json.loads(wire)
        batches=self.warmup_batches(request)  # Validate ALL batches before any send.
        previous=None;sent=0
        for batch in batches[:-1]:
            warm={**request,'generate':False,'input':batch}
            # These are additional transport warmups, not the client's generation
            # request. Do not duplicate its History-ingestion metadata.
            warm.pop('client_metadata',None)
            if previous:warm['previous_response_id']=previous
            warm_wire=ws_frame(warm)
            if len(warm_wire.encode())>=PREFLIGHT_BYTES:
                await self.preflight_ws(session)
            await self.send_wire(session.socket,warm_wire);sent+=len(warm_wire.encode())
            while True:
                event=response_event(await asyncio.wait_for(session.socket.recv(),timeout=120))
                if event is None:raise ValueError('invalid_warmup_event')
                if terminal_event(event):
                    response=event.get('response',{})
                    if event.get('type')=='error':
                        raise WarmupRejected('server explicitly rejected non-generating warmup')
                    if (event.get('type')!='response.completed' or response.get('output')!=[]
                            or not isinstance(response.get('id'),str)):
                        raise ValueError('warmup_not_confirmed')
                    previous=response['id'];session.warmup_count+=1
                    break
        final={**request,'input':batches[-1],'previous_response_id':previous}
        return ws_frame(final),sent

    @staticmethod
    async def send_wire(socket, wire):
        # Bound each frame while preserving ONE WebSocket application message.
        # Large single frames are rejected by some upstream proxy/server stacks.
        if len(wire)>1024*1024:
            await socket.send((wire[i:i+256*1024] for i in range(0,len(wire),256*1024)))
        else:
            await socket.send(wire)

    async def preflight_ws(self,session):
        # Ping is a control frame: it doesn't create a response, alter the
        # continuation baseline, or upload any history. A Pong only proves
        # transport reachability at this instant, not future inference success.
        self.preflight_counts['ws_checks']+=1
        try:
            waiter=await asyncio.wait_for(session.socket.ping(),PREFLIGHT_TIMEOUT)
            await asyncio.wait_for(waiter,PREFLIGHT_TIMEOUT)
        except Exception as error:
            self.preflight_counts['blocked']+=1
            self.diagnostic('preflight_ws',error,pending_payload_sent=False)
            raise UpstreamPreflightFailed('upstream WebSocket preflight failed') from error

    async def preflight_http(self,target,headers):
        # HEAD on the same URL uses the same auth, proxy and TLS validation as
        # the real request but carries no body. 404/405 still prove the HTTP
        # peer responded; they do not certify that a later POST will succeed.
        self.preflight_counts['http_checks']+=1
        probe_headers=CIMultiDict((k,v) for k,v in headers.items() if k.lower() not in
                       {'content-type','content-length','content-encoding','expect'})
        try:
            async with self.http.request('HEAD',target,headers=probe_headers,
                    proxy=self.upstream_proxy,allow_redirects=False,
                    timeout=ClientTimeout(total=PREFLIGHT_TIMEOUT)) as probe:
                if probe.status>=500 or probe.status in (401,403,407,408,425,429) or 300<=probe.status<400:
                    rejected=UpstreamPreflightFailed('upstream HTTP preflight rejected')
                    rejected.status=probe.status
                    raise rejected
        except Exception as error:
            self.preflight_counts['blocked']+=1
            self.diagnostic('preflight_http',error,pending_payload_sent=False)
            raise UpstreamPreflightFailed('upstream HTTP preflight failed') from error

    def listen_port(self):
        return self.site._server.sockets[0].getsockname()[1]

    def authorized(self, req):
        return hmac.compare_digest(req.headers.get('x-uplink-token',''),self.token)

    def session_key(self, request, headers, query=''):
        ident=identity(request)
        principal=principal_hash(headers)
        if not ident or not principal:
            return None
        return (ident,principal,websocket_handshake_hash(headers),query)

    async def session(self, key, headers):
        async with self.guard:
            now=time.monotonic()
            for k,s in list(self.pool.items()):
                if not s.live() or (not s.lock.locked() and s.downstreams==0 and now-s.last_used>self.cache_seconds):
                    self.pool.pop(k)
                    await s.socket.close()
            item=self.pool.get(key)
            if item and item.live():
                item.last_used=now
                return item
            kwargs={'additional_headers':headers_for_ws(headers),'compression':'deflate',
                    'max_size':None,'ping_interval':20,
                    'user_agent_header':None,
                    'proxy':self.upstream_proxy if self.upstream_proxy is not None else None}
            query=key[-1] if isinstance(key[-1],str) else ''
            socket=await connect(with_request_query(self.target_ws,query),**kwargs)
            item=Upstream(key,socket)
            self.pool[key]=item
            return item

    async def incoming(self, req):
        if req.path in ('/_uplink/health','/_uplink/shutdown'):
            if not self.authorized(req):return web.Response(status=403)
            if req.path.endswith('/shutdown'):
                if req.method!='POST' or self.shutdown_event is None:return web.Response(status=405)
                self.shutdown_event.set()
                return web.json_response({'stopping':True})
            return web.json_response({'product':'codex-uplink-optimizer','version':'12.2.0',
                'requests':self.requests_seen,'live_upstreams':sum(s.live() for s in self.pool.values()),
                'preflight':self.preflight_counts})
        if self.native_mount and req.path.startswith('/uplink/'):
            parts=req.path.split('/',3)
            if len(parts)!=4 or not hmac.compare_digest(parts[2],self.token):
                return web.Response(status=403)
            if any(x in ('.','..') for x in parts[3].split('/')):
                return web.Response(status=400)
            # Decoding an encoded slash or question mark changes the resource.
            suffix='/'+req.rel_url.raw_path.split('/',3)[3]
            if suffix=='/responses':
                if req.method=='GET' and req.headers.get('Upgrade','').lower()=='websocket':
                    self.requests_seen['websocket']+=1
                    return await self.incoming_ws(req)
                if req.method=='POST':
                    self.requests_seen['http']+=1
                    return await self.incoming_http(req)
            # Preserve all native HTTP routes, including models and compaction.
            # This is a fixed-origin reverse proxy, never an arbitrary URL proxy.
            if req.headers.get('Upgrade','').lower()=='websocket':
                return web.Response(status=501,text='unsupported native websocket route')
            self.requests_seen['native_other']+=1
            target=self.target_http.split('?',1)[0].rsplit('/',1)[0]+suffix
            return await self.http_native(req,None,reason='http_native_endpoint',target=target)
        if not self.authorized(req):
            return web.Response(status=403,text='invalid local relay token')
        models_url=self.target_http.split('?',1)[0].rsplit('/',1)[0]+'/models'
        if req.method=='GET' and req.path==urlsplit(models_url).path:
            return await self.http_native(req,b'',reason='http_native_models',target=models_url)
        if req.path != urlsplit(self.target_http).path and req.path != urlsplit(self.target_ws).path:
            return web.Response(status=404,text='only configured Responses path supported')
        if req.method=='GET' and req.headers.get('Upgrade','').lower()=='websocket':
            return await self.incoming_ws(req)
        if req.method=='POST':
            return await self.incoming_http(req)
        return web.Response(status=405)

    async def incoming_http(self, req):
        # Huge single requests bypass JSON parsing and stream directly to the
        # native endpoint; do not allocate hundreds of MB only to decide no.
        if (req.content_length is not None and req.content_length > MAX_DECOMPRESSED):
            return await self.http_native(req,None,reason='http_native_oversized_stream')
        # A transport adapter must not turn an unsupported media type or a
        # body-bound signature into a different, apparently valid request.
        if (req.content_type.lower() != 'application/json'
                or (req.charset and req.charset.lower() not in ('utf-8','utf8'))
                or any(name in req.headers for name in BODY_BOUND_HEADERS)):
            return await self.http_native(req,None,reason='http_native_media_or_signature')
        # Raw native HTTP pass-through covers every case the WS gateway cannot
        # prove compatible: unsupported encodings/fields, missing identity,
        # unexpected protocol, handshake failure, etc.
        try:
            raw=await req.read()
            original=decoded_json(raw,req.headers.get('Content-Encoding','identity'))
            converted=http_to_ws(original)
        except (ValueError,UnicodeDecodeError,json.JSONDecodeError):
            converted=None
            raw=await req.read() if not req.content.at_eof() else locals().get('raw',b'')
        if converted is None or self.session_key(converted,req.headers) is None:
            return await self.http_native(req,raw,reason='http_native_passthrough')
        return await self.http_ws(req,raw,converted)

    async def http_native(self,req,raw,reason,target=None):
        # Headers are passed without semantics-altering edits. aiohttp may have
        # read/dechunked the HTTP body; it will set an accurate content length.
        nominated=connection_tokens(req.headers)
        headers=CIMultiDict((k,v) for k,v in req.headers.items() if k.lower() not in
                 (HOP - {'content-length','content-encoding','content-type','accept','accept-encoding','user-agent'}) | nominated)
        # Preserve known wire length while streaming, and don't invent an
        # empty chunked body on body-free GET/HEAD requests.
        payload=raw if raw is not None else req.content.iter_chunked(32768) if req.can_read_body else None
        if not req.can_read_body and not raw:
            payload=None
        started=False
        response=None
        try:
            upstream_url=URL(with_request_query(target or self.target_http,req.rel_url.raw_query_string),encoded=True)
            size=len(raw) if raw is not None else req.content_length
            if (req.method in ('POST','PUT','PATCH') and (size is None or size>=PREFLIGHT_BYTES)
                    and not any(name in headers for name in BODY_BOUND_HEADERS)):
                # A signed POST cannot safely be repurposed into a HEAD probe.
                await self.preflight_http(upstream_url,headers)
            async with self.http.request(req.method,upstream_url,data=payload,headers=headers,
                                         proxy=self.upstream_proxy,
                                         allow_redirects=False) as upstream:
                response_nominated=connection_tokens(upstream.headers)
                h=CIMultiDict((k,v) for k,v in upstream.headers.items() if k.lower() not in
                              HTTP_HOP | response_nominated)
                response=web.StreamResponse(status=upstream.status,headers=h)
                response[NATIVE_HEADER_NAMES]=frozenset(k.lower() for k in h)
                await response.prepare(req)
                started=True
                async for chunk in upstream.content.iter_chunked(32768):
                    await response.write(chunk)
                await response.write_eof()
                size = len(raw) if raw is not None else req.content_length or 0
                metrics_log(self.metrics,reason=reason,before=size,after=size,
                            input_before=0,input_after=0,ingress='http',upstream='http',
                            received_body_bytes=size,content_encoding=req.headers.get('Content-Encoding','identity'))
                return response
        except Exception as err:
            self.diagnostic('native_http',err,response_started=started)
            LOG.warning('native HTTP pass-through failed: %s',type(err).__name__)
            if started:
                # An already-started response cannot be replaced by a second
                # JSON error response. Preserve the failure as a broken stream.
                if req.transport:req.transport.close()
                return response
            blocked=isinstance(err,UpstreamPreflightFailed)
            return web.json_response({'error':{'message':('Codex CATS connectivity check failed; the request body was not sent upstream.' if blocked else 'Codex CATS could not reach the upstream HTTP endpoint. Check DOCTOR and errors.jsonl.'),
                'type':'gateway_error','code':'uplink_preflight_failed' if blocked else 'uplink_native_http_unavailable'}},status=503 if blocked else 502)

    async def http_ws(self,req,raw,request):
        key=self.session_key(request,req.headers,req.rel_url.raw_query_string)
        try:
            session=await self.session(key,req.headers)
        except Exception as err:
            self.diagnostic('http_ws_handshake',err)
            # Handshake failure is prior to sending a request. Native HTTP
            # fallback is safe; nothing was sent to the WS provider.
            return await self.http_native(req,raw,reason='http_fallback_ws_unavailable')
        # A second concurrent request cannot share a live prefix until the
        # first one completes; bound lock wait and use native HTTP instead.
        try:
            await asyncio.wait_for(session.lock.acquire(),timeout=1)
        except asyncio.TimeoutError:
            return await self.http_native(req,raw,reason='http_busy_native_passthrough')
        try:
            if not session.live():
                session.baseline=None
                return await self.http_native(req,raw,reason='http_fallback_ws_closed')
            payload,reason=plan(request,session.baseline,upstream_live=True)
            full_wire=ws_frame(request)
            wire=full_wire if payload is request else ws_frame(payload)
            if len(wire.encode())>WS_SAFE_PAYLOAD:
                try:self.warmup_batches(payload)
                except UnsegmentableInput:
                    return await self.http_native(req,raw,reason='http_native_indivisible_ws_size_limit')
            # SSE is HTTP Responses' event format. Full WS event JSON is kept;
            # no event data or text payload is rewritten.
            started=False
            try:
                response=web.StreamResponse(status=200,headers={
                    'Content-Type':'text/event-stream','Cache-Control':'no-cache',
                    'X-Accel-Buffering':'no'})
                async for chunk,event in self.exchange(session,wire,full_wire,reason=='live_continuation'):
                    if not started:
                        await response.prepare(req)
                        started=True
                    await response.write(b'data: '+chunk.encode('utf-8')+b'\n\n'
                                         if isinstance(chunk,str) else b'data: '+chunk+b'\n\n')
                    if terminal_event(event):
                        if event['type']=='response.completed':
                            session.baseline=baseline_after_success(request,event,session.baseline)
                        else:
                            session.baseline=None
                        break
                await response.write_eof()
                metrics_log(self.metrics,reason='cache_rejected_full_retry' if session.retried_full else ('full_context_warmup' if session.warmup_count else reason),before=len(full_wire.encode()),
                            after=session.sent_bytes,input_before=len(request['input']),
                            input_after=len(request['input']) if session.retried_full else len(payload['input']),
                            ingress='http',upstream='websocket',received_body_bytes=len(raw),
                            content_encoding=req.headers.get('Content-Encoding','identity'),
                            images_before=image_count(request['input']),
                            images_after=image_count(request['input'] if session.retried_full else payload['input']),
                            warmup_count=session.warmup_count,
                            ws_compression='permessage-deflate' if session.socket.protocol.extensions else 'none')
                session.last_used=time.monotonic()
                return response
            except Exception as err:
                self.diagnostic('http_ws_exchange',err,response_started=started)
                session.baseline=None
                try:
                    await session.socket.close()
                except Exception:
                    pass
                close_code=getattr(getattr(err,'rcvd',None),'code',None)
                LOG.warning('HTTP->WS exchange error: %s close_code=%s',type(err).__name__,close_code)
                if not started and isinstance(err,WarmupRejected):
                    return await self.http_native(req,raw,reason='http_fallback_warmup_rejected')
                # No blind HTTP retry here: server may have accepted the request.
                if started:
                    return response
                if isinstance(err,UpstreamPreflightFailed):
                    return web.json_response({'error':{'message':'Codex CATS connectivity check failed; upload stopped before the pending payload.',
                        'type':'gateway_error','code':'uplink_preflight_failed'}},status=503)
                return web.json_response({'error':{'message':'Codex CATS upstream WebSocket execution failed. Request outcome is uncertain; no automatic full replay was attempted.',
                    'type':'gateway_error','code':'uplink_ws_execution_uncertain'}},status=502)
        finally:
            session.lock.release()

    async def incoming_ws(self,req):
        sock=web.WebSocketResponse(max_msg_size=0,heartbeat=20,compress=False)
        await sock.prepare(req)
        session=None
        locked=False
        held=None
        upstream_closed=None
        async def notify_upstream_closed(upstream):
            # Native Codex owns its previous_response_id chain. A local socket
            # must not hide loss of the upstream connection that owns that ID.
            await upstream.socket.wait_closed()
            if not sock.closed:
                await sock.close(code=1012,message=b'upstream closed; reconnect required')
        async def attach(new):
            nonlocal held, upstream_closed
            if held is new:return
            if upstream_closed:
                upstream_closed.cancel()
                await asyncio.gather(upstream_closed,return_exceptions=True)
            if held:
                held.downstreams-=1
                if held.key[0]=='dedicated':
                    await held.socket.close()
                    self.pool.pop(held.key,None)
            held=new
            held.downstreams+=1
            upstream_closed=asyncio.create_task(notify_upstream_closed(new))
        try:
            async for message in sock:
                if message.type not in (WSMsgType.TEXT,WSMsgType.BINARY):
                    if message.type==WSMsgType.ERROR:break
                    continue
                wire=message.data
                item=response_event(wire)
                if item is None or item.get('type')!='response.create':
                    await sock.close(code=1003,message=b'response.create required')
                    break
                key=self.session_key(item,req.headers,req.rel_url.raw_query_string)
                if key is None:
                    # Dedicated connection: unknown identity prevents reuse, not
                    # normal forwarding. Never share this scope with another client.
                    key=('dedicated', id(sock),req.rel_url.raw_query_string)
                if session is not None and not session.live():
                    # Cover the race before the close watcher can notify Codex.
                    # Never forward caller-owned incremental input on a new WS.
                    await sock.close(code=1012,message=b'upstream closed; reconnect required')
                    break
                if session is not None and key != session.key:
                    if item.get('previous_response_id'):
                        await sock.close(code=1012,message=b'upstream scope changed; reconnect required')
                        break
                    session=None
                if session is None:
                    try:
                        session=await self.session(key,req.headers)
                    except Exception as err:
                        self.diagnostic('ws_handshake',err)
                        await sock.close(code=1011,message=b'upstream unavailable')
                        break
                await attach(session)
                try:
                    await asyncio.wait_for(session.lock.acquire(),timeout=1)
                except asyncio.TimeoutError:
                    # A concurrent response owns the shared socket. Full-forward
                    # on a separate socket instead of closing or corrupting it.
                    session=await self.session(('dedicated',id(sock),req.rel_url.raw_query_string),req.headers)
                    await attach(session)
                    await session.lock.acquire()
                    key=session.key
                locked=True
                if key != session.key or not session.live():
                    session.baseline=None
                    await sock.close(code=1008,message=b'changed scope or closed upstream')
                    break
                payload,reason=plan(item,session.baseline,upstream_live=True)
                send=ws_frame(payload) if reason=='live_continuation' else wire
                if isinstance(wire,bytes) and isinstance(send,str):send=send.encode('utf-8')
                # Stream all response events unmodified. State is confirmed only
                # after a COMPLETE response was successfully delivered to client.
                async for ev_wire,event in self.exchange(session,send,wire,reason=='live_continuation'):
                    if isinstance(ev_wire,bytes):
                        await sock.send_bytes(ev_wire)
                    else:
                        await sock.send_str(ev_wire)
                    if terminal_event(event):
                        session.baseline=(baseline_after_success(item,event,session.baseline)
                                          if event['type']=='response.completed' else None)
                        break
                size=lambda x:len(x if isinstance(x,bytes) else x.encode())
                metrics_log(self.metrics,reason='cache_rejected_full_retry' if session.retried_full else ('full_context_warmup' if session.warmup_count else reason),
                            before=size(wire),after=session.sent_bytes,
                            input_before=len(item.get('input',[])),input_after=len(item.get('input',[])) if session.retried_full else len(payload.get('input',[])),
                            ingress='websocket',upstream='websocket',received_body_bytes=size(wire),
                            content_encoding='none',images_before=image_count(item.get('input',[])),
                            images_after=image_count(item.get('input',[]) if session.retried_full else payload.get('input',[])),
                            warmup_count=session.warmup_count,
                            ws_compression='permessage-deflate' if session.socket.protocol.extensions else 'none')
                session.last_used=time.monotonic()
                session.lock.release()
                locked=False
        except Exception as err:
            self.diagnostic('ws_exchange',err)
            LOG.warning('WS downstream/upstream failed: %s',type(err).__name__)
            if isinstance(err,UpstreamPreflightFailed) and not sock.closed:
                await sock.send_json({'type':'error','status':503,'error':{
                    'type':'gateway_error','code':'uplink_preflight_failed',
                    'message':'Connectivity check failed; upload stopped before the pending payload.'}})
                await sock.close(code=1013,message=b'upstream preflight failed')
            if session and locked:
                session.baseline=None
                try:await session.socket.close()
                except Exception:pass
        finally:
            if upstream_closed:
                upstream_closed.cancel()
                await asyncio.gather(upstream_closed,return_exceptions=True)
            if held:held.downstreams-=1
            if session:
                session.last_used=time.monotonic()
                if locked:
                    session.lock.release()
                if session.key[0]=='dedicated':
                    await session.socket.close()
                    self.pool.pop(session.key,None)
        return sock


async def main_async(args):
    gw=Gateway(target_ws=args.target_ws,target_http=args.target_http,token=args.token,
               host=args.host,port=args.port,upstream_proxy=args.proxy,
               cache_seconds=args.cache_seconds,metrics=args.metrics)
    await gw.start()
    print(f'Codex v12 unified gateway listening at http://{args.host}:{gw.listen_port()}',flush=True)
    try:await asyncio.Future()
    finally:await gw.close()


def main():
    p=argparse.ArgumentParser(description='Cross-transport, no-image-deletion Codex uplink gateway')
    p.add_argument('--target-ws',required=True,help='Explicit WS provider endpoint')
    p.add_argument('--target-http',required=True,help='Explicit HTTP provider endpoint')
    p.add_argument('--token',required=True,help='16+ character local gateway token')
    p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=8766)
    p.add_argument('--proxy',default=None,help='Existing upstream HTTP/SOCKS proxy, explicit')
    p.add_argument('--cache-seconds',type=int,default=90)
    p.add_argument('--metrics',default=None)
    p.add_argument('--verbose',action='store_true')
    opts=p.parse_args()
    logging.basicConfig(level=logging.INFO if opts.verbose else logging.WARNING)
    asyncio.run(main_async(opts))


if __name__=='__main__':main()

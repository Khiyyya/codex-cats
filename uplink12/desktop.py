"""Per-user Windows lifecycle. All control messages stay on loopback."""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from .gateway import Gateway
from .integration import (PRODUCT, activate, atomic, autorun, autorun_value, codex_config,
                          detect_proxy, discover, load_json, restore, save_json, activation_state, digest)
from .metrics import status as metric_status

def default_home():
    # Resolve MSIX filesystem redirection so login startup outside the package
    # addresses the same installation created from inside Codex Desktop.
    return (Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'CodexUplink').resolve()
def settings(home):return load_json(home/'settings.json')
def base_url(cfg):return f"http://127.0.0.1:{cfg['port']}/uplink/{cfg['local_token']}"
def control(home, action='health'):
    cfg=settings(home)
    req=urllib.request.Request(f"http://127.0.0.1:{cfg['port']}/_uplink/{action}",
        headers={'x-uplink-token':cfg['local_token']},method='POST' if action=='shutdown' else 'GET')
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=2) as r:
        data=json.load(r)
    if action=='health' and data.get('product')!=PRODUCT:raise RuntimeError('unexpected listener')
    return data

def daemon_command(home, windowless=False):
    exe=Path(sys.executable).resolve()
    if windowless and os.name=='nt':exe=exe.with_name('pythonw.exe')
    return [str(exe),'-m','uplink12.desktop','--home',str(home),'daemon']

def login_command(home):
    # Let the logon launcher ask the independent task service to start the
    # daemon, rather than running an unmonitored daemon under Explorer.
    return subprocess.list2cmdline(daemon_command(home,True)[:-1]+['start'])

def owned_autorun_commands(home,cfg):
    commands={subprocess.list2cmdline(daemon_command(home,True)),login_command(home)}
    if cfg.get('autorun_command'):commands.add(cfg['autorun_command'])
    # Upgrade the first MSIX install which recorded the redirected logical path.
    logical=Path(os.environ.get('LOCALAPPDATA',''))/'CodexUplink/runtime/Scripts/pythonw.exe'
    if logical.resolve()==Path(daemon_command(home,True)[0]).resolve():
        commands.add(subprocess.list2cmdline([str(logical),'-m','uplink12.desktop','--home',str(home),'daemon']))
    return commands

def ensure_started(home):
    try:return control(home)
    except Exception:pass
    # Never kill or replace another process occupying our port.
    cfg=settings(home)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',cfg['port']))
    if os.name=='nt':
        from .background import start
        start(home,daemon_command(home,True))
    else:
        subprocess.Popen(daemon_command(home,True),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,cwd=home)
    for _ in range(80):
        try:return control(home)
        except Exception:time.sleep(.1)
    raise RuntimeError('gateway failed to start; see sanitized lifecycle log')

class InstanceLock:
    def __init__(self,path):self.path=path;self.f=None
    def __enter__(self):
        self.f=open(self.path,'a+b');self.f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                if self.path.stat().st_size==0:self.f.write(b'0');self.f.flush()
                self.f.seek(0);msvcrt.locking(self.f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:
            self.f.close();raise
        return self
    def __exit__(self,*exc):self.f.close()

async def serve(home):
    cfg=settings(home);stop=asyncio.Event()
    gw=Gateway(target_http=cfg['target_http'],target_ws=cfg['target_ws'],
        token=cfg['local_token'],port=cfg['port'],upstream_proxy=cfg['upstream_proxy'],
        cache_seconds=300,metrics=str(home/'metrics.jsonl'),native_mount=True,shutdown_event=stop)
    await gw.start()
    save_json(home/'runtime.json',{'pid':os.getpid(),'started':time.time()})
    try:
        if cfg.get('enabled'):activate(Path(cfg['config']),home,base_url(cfg))
        await stop.wait()
    finally:await gw.close()

def run_daemon(home):
    with InstanceLock(home/'daemon.lock'):
        try:
            # Re-detect the existing proxy at each login; never change it.
            cfg=settings(home)
            cfg['upstream_proxy'],cfg['proxy_source']=detect_proxy()
            save_json(home/'settings.json',cfg)
            asyncio.run(serve(home))
        except Exception as err:
            atomic(home/'last-error.txt',(type(err).__name__+'; gateway unavailable; run START or STOP to recover\n').encode())
            raise
        finally:
            restore(home)
            (home/'runtime.json').unlink(missing_ok=True)

def install(home):
    if os.environ.get('OPENAI_BASE_URL'):
        raise ValueError('OPENAI_BASE_URL environment override detected; automatic integration refused')
    auth_path=codex_config().parent/'auth.json'
    if auth_path.exists():
        # Inspect login type only. Never copy tokens into installation state.
        auth_mode=load_json(auth_path).get('auth_mode')
        if auth_mode not in (None,'chatgpt'):
            raise ValueError('automatic integration currently requires ChatGPT login')
    found=discover()
    if not found['desktop'] or not found['core']:raise RuntimeError('official Windows Codex Desktop not found')
    file=home/'settings.json'
    previous_settings=file.read_bytes() if file.exists() else None
    if previous_settings is None:
        proxy,source=detect_proxy()
        with socket.socket() as s:
            try:s.bind(('127.0.0.1',8766))
            except OSError:s.bind(('127.0.0.1',0))
            port=s.getsockname()[1]
        cfg={'product':PRODUCT,'port':port,'local_token':secrets.token_urlsafe(32),
            'target_http':'https://chatgpt.com/backend-api/codex/responses',
            'target_ws':'wss://chatgpt.com/backend-api/codex/responses',
            'upstream_proxy':proxy,'proxy_source':source,'config':str(codex_config().resolve())}
    else:cfg=json.loads(previous_settings)
    cfg.setdefault('proxy_source','unknown')
    command=login_command(home)
    old=autorun_value()
    if old is not None and old not in owned_autorun_commands(home,cfg):
        raise RuntimeError('autostart value owned by another installation')
    # Refuse conflicting user configuration before creating tasks or settings.
    config=Path(cfg['config'])
    activation_state(config,home,base_url(cfg))
    watched=(config,home/'config-ownership.json',home/'config-original.toml')
    snapshot={path:path.read_bytes() if path.exists() else None for path in watched}
    was_running=healthy(home) if previous_settings is not None else False
    home.mkdir(parents=True,exist_ok=True)
    if previous_settings is None:save_json(file,cfg)
    activated_hash=None
    try:
        ensure_started(home)
        activate(config,home,base_url(cfg))
        activated_hash=digest(config.read_bytes())
        autorun(command)
        cfg['enabled']=True;cfg['autorun_command']=command;save_json(file,cfg)
    except Exception:
        # Do not overwrite a configuration changed concurrently by the user.
        current=config.read_bytes() if config.exists() else None
        if current==snapshot[config] or (activated_hash and current is not None and digest(current)==activated_hash):
            for path,raw in snapshot.items():
                if raw is None:path.unlink(missing_ok=True)
                else:atomic(path,raw)
        elif snapshot[home/'config-ownership.json'] is None:
            restore(home)
        if autorun_value()==command and old!=command:
            autorun(old)
        if not was_running:
            try:control(home,'shutdown')
            except Exception:pass
            for _ in range(50):
                if not healthy(home):break
                time.sleep(.1)
            if healthy(home):
                cfg['enabled']=False;save_json(file,cfg)
                raise RuntimeError('installation failed; gateway cleanup incomplete, runtime retained')
            if previous_settings is None and os.name=='nt':
                from .background import remove
                remove(home)
        if previous_settings is None:file.unlink(missing_ok=True)
        else:atomic(file,previous_settings)
        raise
    return {'installed':True,'desktop_version':found['desktop']['Version'],
        'autostart':autorun_value()==command,'gateway_ready':True,
        'activation':'restart existing Codex once; new processes read managed configuration',
        'config':cfg['config'],'home':str(home),'proxy_source':cfg['proxy_source']}

def disable(home):
    # Restore first: if a concurrent edit prevents recovery, leave the working
    # gateway and startup state available rather than stranding the client.
    cfg=settings(home)
    outcome=restore(home)
    cfg['enabled']=False;save_json(home/'settings.json',cfg)
    command=login_command(home)
    if autorun_value() in owned_autorun_commands(home,cfg):autorun()
    try:control(home,'shutdown')
    except Exception:pass
    for _ in range(50):
        try:control(home)
        except Exception:break
        time.sleep(.1)
    if healthy(home):
        raise RuntimeError('gateway is still running; runtime retained, retry stop')
    if os.name=='nt':
        from .background import remove
        remove(home)
    return {'configuration':outcome,'autostart_removed':autorun_value()!=command,'stopped':not healthy(home)}

def healthy(home):
    try:control(home);return True
    except Exception:return False

def report(home):
    cfg=settings(home)
    try:health=control(home)
    except Exception:health=None
    import tomllib
    path=Path(cfg['config'])
    doc=tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    return {'home':str(home),'gateway':health,'configuration_managed':doc.get('openai_base_url')==base_url(cfg),
        'autostart':autorun_value()==cfg.get('autorun_command',login_command(home)),
        'proxy_source':cfg['proxy_source'],'metrics_kind':'application payload; NOT TLS/network bytes',
        'metrics':metric_status(home/'metrics.jsonl')}

def doctor(home):
    result=report(home);found=discover();cfg=settings(home)
    result['desktop_detected']=bool(found['desktop'])
    result['official_core_detected']=bool(found['core'])
    result['desktop_version']=found['desktop']['Version'] if found['desktop'] else None
    error_file=home/'errors.jsonl'
    result['last_gateway_error']=None
    result['startup_error']=None
    startup=home/'last-error.txt'
    if startup.exists():
        # No dynamic exception messages or paths are returned.
        name=startup.read_text(encoding='utf-8',errors='replace').split(';',1)[0].strip()
        if name.isidentifier() and len(name)<=64:result['startup_error']=name
    if error_file.exists():
        try:
            last=json.loads(error_file.read_text(encoding='utf-8').splitlines()[-1])
            result['last_gateway_error']={k:last.get(k) for k in ('time','phase','exception','errno','http_status','ws_close_code')}
        except (ValueError,IndexError):pass
    proxy=cfg.get('upstream_proxy')
    if proxy:
        from urllib.parse import urlsplit
        u=urlsplit(proxy)
        try:
            with socket.create_connection((u.hostname,u.port or (443 if u.scheme=='https' else 80)),timeout=2):pass
            result['proxy_tcp_reachable']=True
        except OSError:result['proxy_tcp_reachable']=False
    else:result['proxy_tcp_reachable']=None
    result['verification_limits']=['GUI History not verified','TCP reachability does not verify authenticated upstream inference',
        'registered autostart does not prove a Windows logoff/logon cycle']
    return result

def open_codex(home):
    install(home)
    app=discover()['desktop']
    import xml.etree.ElementTree as ET
    root=ET.parse(Path(app['InstallLocation'])/'AppxManifest.xml').getroot()
    apps=root.findall('.//{*}Application')
    appid=next((a.attrib['Id'] for a in apps if a.attrib.get('Executable','').lower().endswith('chatgpt.exe')),None)
    if not appid:raise RuntimeError('Codex launch identity not found')
    subprocess.Popen(['explorer.exe',f"shell:AppsFolder\\{app['PackageFamilyName']}!{appid}"],creationflags=subprocess.CREATE_NO_WINDOW)
    return {'launch_requested':True}

def main():
    p=argparse.ArgumentParser();p.add_argument('--home',type=Path,default=default_home())
    p.add_argument('command',choices=['install','start','stop','uninstall','status','doctor','daemon','open'])
    a=p.parse_args();home=a.home.resolve()
    # Never enable verbose HTTP logs: they can contain local mount tokens.
    logging.basicConfig(level=logging.CRITICAL)
    try:
        if a.command=='daemon':run_daemon(home);return
        if a.command in ('install','start'):result=install(home)
        elif a.command in ('stop','uninstall'):
            result=disable(home)
            if a.command=='uninstall':
                result['runtime_retained']=str(home)
                result['note']='integration removed; runtime can now be removed with UNINSTALL.cmd'
        elif a.command=='open':result=open_codex(home)
        elif a.command=='doctor':result=doctor(home)
        else:result=report(home)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as err:
        # Only known operator errors are human readable; never print URLs/headers.
        safe=str(err) if isinstance(err,(ValueError,RuntimeError)) else type(err).__name__
        print(json.dumps({'error':safe},ensure_ascii=False));sys.exit(1)

if __name__=='__main__':main()

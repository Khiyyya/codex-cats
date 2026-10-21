"""Discovery and owned, reversible edits. No account credentials are loaded."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
from urllib.parse import urlsplit
import tomlkit

PRODUCT='codex-uplink-optimizer'
RUN_NAME='CodexUplinkOptimizer'

def atomic(path, raw):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.uplink-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

def save_json(path,data):atomic(path,(json.dumps(data,indent=2)+'\n').encode())
def load_json(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def digest(raw):return hashlib.sha256(raw).hexdigest()
def codex_config():return Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'config.toml'

def activation_state(config, state_dir, value):
    """Read-only validation, repeated immediately before the owned edit."""
    config=Path(config);state_dir=Path(state_dir)
    raw=config.read_bytes() if config.exists() else b''
    doc=tomlkit.parse(raw.decode('utf-8-sig'))
    selected=doc.get('profile')
    profiles=doc.get('profiles',{})
    profile=profiles.get(selected,{}) if isinstance(profiles,dict) and selected else {}
    if profile.get('model_provider',doc.get('model_provider','openai'))!='openai':
        raise ValueError('custom provider detected; automatic integration refused')
    if profile.get('openai_base_url') is not None:
        raise ValueError('profile base URL detected; automatic integration refused')
    marker=state_dir/'config-ownership.json'
    if marker.exists():
        owner=load_json(marker)
        if str(config.resolve())!=owner['config']:raise ValueError('different CODEX_HOME; restore first')
        current=doc.get('openai_base_url')
        if current not in (owner['value'],owner['original']):
            raise ValueError('openai_base_url changed by user; preserving it')
    else:
        original=doc.get('openai_base_url')
        if original is not None:
            raise ValueError('existing openai_base_url detected; automatic integration refused')
        owner={'config':str(config.resolve()),'existed':config.exists(),
               'original':original,'value':value,'installed_hash':None}
    return raw,doc,owner,marker.exists()


def activate(config, state_dir, value):
    config=Path(config);state_dir=Path(state_dir)
    raw,doc,owner,existing=activation_state(config,state_dir,value)
    marker=state_dir/'config-ownership.json'
    if not existing:
        state_dir.mkdir(parents=True,exist_ok=True)
        atomic(state_dir/'config-original.toml',raw)
    doc['openai_base_url']=value
    rendered=tomlkit.dumps(doc).encode()
    tomllib.loads(rendered.decode())
    owner['value']=value;owner['installed_hash']=digest(rendered)
    save_json(marker,owner) # Recovery metadata is durable BEFORE changing config.
    if (config.read_bytes() if config.exists() else b'')!=raw:
        raise ValueError('configuration changed concurrently; retry')
    atomic(config,rendered)

def restore(state_dir):
    state_dir=Path(state_dir);marker=state_dir/'config-ownership.json'
    if not marker.exists():return 'no_owned_configuration'
    owner=load_json(marker);config=Path(owner['config'])
    raw=config.read_bytes() if config.exists() else b''
    doc=tomlkit.parse(raw.decode('utf-8-sig'))
    if doc.get('openai_base_url')!=owner['value']:
        marker.unlink()
        return 'user_change_preserved'
    if digest(raw)==owner['installed_hash']:
        if owner['existed']:atomic(config,(state_dir/'config-original.toml').read_bytes())
        else:config.unlink(missing_ok=True)
    else:
        if owner['original'] is None:doc.pop('openai_base_url',None)
        else:doc['openai_base_url']=owner['original']
        if config.read_bytes()!=raw:raise ValueError('configuration changed concurrently; retry')
        atomic(config,tomlkit.dumps(doc).encode())
    marker.unlink()
    return 'restored'

def parse_proxy(value):
    value=value.strip()
    if ';' in value or ('=' in value and '://' not in value):
        entries=dict(p.split('=',1) for p in value.split(';') if '=' in p)
        value=entries.get('https') or entries.get('http') or ''
        if not value:raise ValueError('no HTTP(S) proxy in system proxy settings')
    if '://' not in value:value='http://'+value
    u=urlsplit(value)
    if u.scheme not in ('http','https') or not u.hostname or u.username or u.password:
        raise ValueError('unsupported proxy; HTTP(S) without embedded credentials required')
    if u.path not in ('','/') or u.query or u.fragment:raise ValueError('invalid proxy URL')
    _=u.port
    return value.rstrip('/')

def detect_proxy():
    for key in ('HTTPS_PROXY','https_proxy'):
        if os.environ.get(key):return parse_proxy(os.environ[key]),'environment'
    if os.name=='nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            def get(name,default):
                try:return winreg.QueryValueEx(key,name)[0]
                except FileNotFoundError:return default
            if get('ProxyEnable',0):return parse_proxy(get('ProxyServer','')),'system'
            if get('AutoConfigURL',''):raise ValueError('PAC detected; set an explicit HTTP(S) proxy')
    for key in ('HTTP_PROXY','http_proxy'):
        if os.environ.get(key):return parse_proxy(os.environ[key]),'environment'
    return None,'direct'

def discover():
    core=list((Path(os.environ.get('LOCALAPPDATA',''))/'OpenAI'/'Codex'/'bin').glob('*/codex.exe'))
    core=sorted(core,key=lambda p:p.stat().st_mtime,reverse=True)
    result={'core':str(core[0]) if core else None,'desktop':None}
    if os.name=='nt':
        command='Get-AppxPackage OpenAI.Codex | Select-Object -First 1 Name,Version,InstallLocation,PackageFamilyName | ConvertTo-Json -Compress'
        p=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=20)
        if p.returncode==0 and p.stdout.strip():result['desktop']=json.loads(p.stdout)
    return result

def autorun(command=None):
    if os.name!='nt':raise RuntimeError('Windows required')
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        if command is not None:winreg.SetValueEx(key,RUN_NAME,0,winreg.REG_SZ,command)
        else:
            try:winreg.DeleteValue(key,RUN_NAME)
            except FileNotFoundError:pass

def autorun_value():
    if os.name!='nt':return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            return winreg.QueryValueEx(key,RUN_NAME)[0]
    except FileNotFoundError:return None

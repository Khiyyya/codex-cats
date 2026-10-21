"""Start outside the Desktop process tree using the current user's task service.

An on-demand, limited, interactive-user task; no password, elevation or network
configuration changes. Login autostart remains separately owned by the manager.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
from xml.sax.saxutils import escape

DESCRIPTION='Codex CATS owned on-demand gateway runner v1'

def task_name(home):
    scope=str(Path(home).resolve()).casefold()+os.environ.get('USERNAME','')
    return 'CodexCATS-Gateway-'+hashlib.sha256(scope.encode()).hexdigest()[:12]

def powershell(script):
    encoded=base64.b64encode(script.encode('utf-16le')).decode()
    p=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
    if p.returncode:raise RuntimeError('independent Windows task startup failed; no configuration activation performed')

def start(home,command):
    # Every dynamic string enters XML or a JSON literal, never raw shell code.
    executable=escape(command[0]);arguments=escape(subprocess.list2cmdline(command[1:]))
    working=escape(str(home));name=task_name(home)
    xml=f'''<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
      <RegistrationInfo><Description>{DESCRIPTION}</Description></RegistrationInfo>
      <Principals><Principal id="User"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
      <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
      <Actions Context="User"><Exec><Command>{executable}</Command><Arguments>{arguments}</Arguments><WorkingDirectory>{working}</WorkingDirectory></Exec></Actions></Task>'''
    payload=base64.b64encode(xml.encode()).decode()
    script=f'''$ErrorActionPreference='Stop'
    $name='{name}'
    $existing=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing -and $existing.Description -ne '{DESCRIPTION}') {{ throw 'Task ownership conflict' }}
    $xml=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))
    Register-ScheduledTask -TaskName $name -Xml $xml -Force | Out-Null
    Start-ScheduledTask -TaskName $name
    '''
    powershell(script)
    return name

def remove(home):
    name=task_name(home)
    powershell(f'''$ErrorActionPreference='Stop'
    $task=Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue
    if ($task -and $task.Description -eq '{DESCRIPTION}') {{ Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false }}''')

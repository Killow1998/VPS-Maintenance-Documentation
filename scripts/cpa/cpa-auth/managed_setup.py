#!/usr/bin/env python3
"""Supervise setup-token, open only a live callback URL, import privately over SSH."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from urllib.parse import parse_qs, urlsplit


REMOTE = r'''
import json, os, sys, subprocess, tempfile, shutil, socket, time
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import quote
from urllib.error import HTTPError
from pathlib import Path

job=json.load(sys.stdin)
config=json.loads(Path(job['observer_config']).read_text())
base=config['cpa_base_url'].rstrip('/')
opener=build_opener(ProxyHandler({}))
def call(path, data=None, method=None):
    headers={'Authorization':'Bearer '+config['management_key']}
    body=None
    if data is not None:
        headers['Content-Type']='application/json'
        body=json.dumps(data).encode()
    req=Request(base+path,data=body,headers=headers,method=method)
    with opener.open(req,timeout=75) as r:
        return json.loads(r.read(4*1024*1024))
name=job['credential_name']
files=call('/v8/management/credentials').get('files',[])
exists=any(f.get('name')==name for f in files)
if job['action']=='preflight':
    print(json.dumps({'ready':not exists,'existing_name':exists}))
    sys.exit(0)
if exists and job['action']!='validate':
    print(json.dumps({'error':'credential_name_exists'}));sys.exit(2)
if job['action']=='validate':
    if not exists:raise RuntimeError('credential_missing')
    job['credential']=call('/v8/management/credentials/download?name='+quote(name))
else:
    staged=dict(job['credential'],disabled=True)
    call('/v8/management/credentials?name='+quote(name),staged,method='POST')
oauth=call('/v8/management/config/oauth')
authdir=oauth.get('auth-dir') if isinstance(oauth,dict) else None
if authdir:
    path=Path(os.path.expanduser(authdir))/name
    if path.is_file(): os.chmod(path,0o600)
files=call('/v8/management/credentials').get('files',[])
record=next((f for f in files if f.get('name')==name),None)
if not record or not record.get('auth_index'):
    print(json.dumps({'imported':True,'verified':False,'error':'credential_not_loaded'}));sys.exit(3)
headers={'Authorization':'Bearer $TOKEN$','anthropic-beta':'oauth-2025-04-20',
         'anthropic-version':'2023-06-01','Content-Type':'application/json',
         'User-Agent':'claude-cli/2.1.310 (external, cli)'}
def upstream(url,method='GET',data=None):
    payload={'auth_index':record['auth_index'],'method':method,'url':url,'header':headers}
    if data is not None:payload['data']=json.dumps(data)
    return call('/v8/management/requests/api-call',payload)
def check_normal_cpa():
    # The management api-call endpoint does not run the Claude executor's normal
    # request preparation. Validate in an isolated native CPA with this account
    # alone, without putting an unverified account into production routing.
    cfg=call('/v8/management/config')
    pid=subprocess.check_output(['systemctl','show','cliproxyapi','--property=MainPID','--value'],text=True).strip()
    binary=os.readlink('/proc/'+pid+'/exe')
    work=Path(tempfile.mkdtemp(prefix='cpa-auth-check-'))
    proc=None
    try:
        os.chmod(work,0o700)
        auth=work/'auth';auth.mkdir(mode=0o700)
        path=auth/name
        path.write_text(json.dumps(dict(job['credential'],disabled=False)))
        path.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        key=os.urandom(32).hex()
        cfg['server'].update(host='127.0.0.1',port=port,tls={'enable':False},discovery={'enabled':False})
        cfg['access']['api-keys']=[key]
        cfg['oauth']['auth-dir']=str(auth)
        cfg['management']={'secret-key':'','allow-remote':False,'disable-control-panel':True}
        cfg['observability']={'logs':{'logging-to-file':False,'request-log':False},'usage':{'usage-statistics-enabled':False}}
        cfgpath=work/'config.yaml';cfgpath.write_text(json.dumps(cfg));cfgpath.chmod(0o600)
        proc=subprocess.Popen([binary,'--config',str(cfgpath),'--local-model'],cwd=work,
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        root='http://127.0.0.1:'+str(port)
        def probe(path,data=None):
            h={'Authorization':'Bearer '+key,'Content-Type':'application/json','anthropic-version':'2023-06-01'}
            try:
                with opener.open(Request(root+path,headers=h,data=json.dumps(data).encode() if data is not None else None),timeout=45) as r:
                    return r.status,json.loads(r.read(4*1024*1024))
            except HTTPError as error:
                try:body=json.loads(error.read())
                except Exception:body={}
                return error.code,body
        registered=False
        for _ in range(100):
            if proc.poll() is not None:raise RuntimeError('isolated_cpa_exited')
            try:
                _,models=probe('/v1/models')
                if any(m.get('id')==job['model'] for m in models.get('data',[])):
                    registered=True;break
            except OSError:pass
            time.sleep(.2)
        if not registered:return None,{}
        return probe('/v1/messages',{'model':job['model'],'max_tokens':64,
                     'messages':[{'role':'user','content':'Reply only OK.'}]})
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        shutil.rmtree(work)
generation_status,body=check_normal_cpa()
quota=upstream('https://api.anthropic.com/api/oauth/usage')
model_ok=generation_status==200 and any(c.get('type')=='text' and c.get('text') for c in body.get('content',[]))
profile=upstream('https://api.anthropic.com/api/oauth/profile')
raw=profile.get('body',{})
try:profile_body=json.loads(raw) if isinstance(raw,str) else raw
except (ValueError,TypeError):profile_body={}
account=profile_body.get('account',{}) if isinstance(profile_body,dict) else {}
identity_ok=profile.get('status_code')==200 and bool(account.get('uuid')) and str(account.get('email','')).casefold()==str(job.get('expected_email','')).casefold()
raw=quota.get('body',{})
try:quota_body=json.loads(raw) if isinstance(raw,str) else raw
except (ValueError,TypeError):quota_body={}
quota_ok=quota.get('status_code')==200 and isinstance(quota_body,dict) and any(isinstance(quota_body.get(w),dict) for w in ('five_hour','seven_day'))
accepted=model_ok and identity_ok and quota_ok
if accepted and not job.get('defer_enable'):
    call('/v8/management/credentials/status',{'name':name,'disabled':False},method='PATCH')
print(json.dumps({'imported':True,'loaded':True,'generation_status':generation_status,
    'has_text':any(c.get('type')=='text' and c.get('text') for c in body.get('content',[])),
    'quota_status':quota.get('status_code'),'profile_status':profile.get('status_code'),
    'identity_verified':identity_ok,'quota_verified':quota_ok,'enabled':accepted and not job.get('defer_enable')}))
'''


def remote(config, action, credential=None):
    job={k:config[k] for k in ('observer_config','credential_name','model','expected_email')}
    job.update(action=action,credential=credential,defer_enable=config.get('defer_enable',False))
    proc=subprocess.run(config['ssh']+[shlex.join(config.get('remote_python',['python3']))+' -c '+shlex.quote(REMOTE)],
                        input=json.dumps(job),text=True,capture_output=True,timeout=190)
    if proc.returncode:
        raise RuntimeError('remote_'+action+'_failed')
    return json.loads(proc.stdout)


def stop_session(*_):
    raise KeyboardInterrupt()


def main():
    signal.signal(signal.SIGTERM, stop_session)
    parser=argparse.ArgumentParser()
    parser.add_argument('--deployment-config',required=True)
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--timeout',type=int,default=1200)
    parser.add_argument('--open-browser',action='store_true',help='Only when explicitly requested by the user')
    args=parser.parse_args()
    os.umask(0o077)
    state=Path(args.state_dir)
    state.mkdir(mode=0o700,parents=True,exist_ok=True)
    def event(phase,**extra):
        result={'phase':phase,'updated_at':time.time(),**extra}
        temp=state/'status.tmp'
        temp.write_text(json.dumps(result));os.replace(temp,state/'status.json')
        print(json.dumps(result),flush=True)
    config=json.loads(Path(args.deployment_config).read_text())
    if not config.get('expected_email'):
        raise RuntimeError('expected_account_email_required')
    if not re.fullmatch(r'[A-Za-z0-9._-]+\.json',config['credential_name']):
        raise RuntimeError('invalid_credential_name')
    event('checking_vps')
    check=remote(config,'preflight')
    if not check.get('ready'):
        raise RuntimeError('credential_name_exists')
    work=Path(tempfile.mkdtemp(prefix='private-',dir=state))
    (work/'claude-config').mkdir(mode=0o700)
    hook=work/'capture-browser'
    hook.write_text('#!'+sys.executable+'\nimport os,sys\nfrom pathlib import Path\np=Path(os.environ["CPA_LOGIN_URL_FILE"])\nt=p.with_suffix(".tmp")\nt.write_text(sys.argv[-1])\nos.replace(t,p)\n')
    hook.chmod(0o700)
    env={k:v for k,v in os.environ.items()
         if not k.startswith(('CLAUDE_','ANTHROPIC_','SSH_'))}
    env.update(CLAUDE_CONFIG_DIR=str(work/'claude-config'),BROWSER=str(hook),
               CPA_LOGIN_URL_FILE=str(work/'browser-url.private'),TERM='xterm-256color',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    binary=shutil.which('claude')
    if not binary:raise RuntimeError('claude_not_installed')
    pid,fd=pty.fork()
    if pid==0:
        os.chdir(work)
        os.execve(binary,[binary,'setup-token'],env)
    fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',40,4096,0,0))
    (state/'pid').write_text(str(os.getpid()))
    event('starting_login')
    captured=bytearray()
    opened=False
    deadline=time.monotonic()+args.timeout
    ansi=re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]')
    imported=False
    try:
        while time.monotonic()<deadline:
            if select.select([fd],[],[],0.25)[0]:
                try: chunk=os.read(fd,65536)
                except OSError:chunk=b''
                if chunk:captured.extend(chunk)
                else:raise RuntimeError('login_process_exited_before_token')
            urlfile=work/'browser-url.private'
            if not opened and urlfile.exists():
                url=urlfile.read_text().strip()
                parsed=urlsplit(url)
                if parsed.scheme!='https' or parsed.hostname not in ('claude.ai','claude.com','console.anthropic.com','platform.claude.com'):
                    event('authorization_host_check',host=parsed.hostname)
                    raise RuntimeError('unexpected_authorization_host')
                redirect=urlsplit(parse_qs(parsed.query).get('redirect_uri',[''])[0])
                if redirect.scheme!='http' or redirect.hostname not in ('localhost','127.0.0.1','::1') or not redirect.port:
                    raise RuntimeError('automatic_local_callback_required')
                with socket.create_connection((redirect.hostname,redirect.port),timeout=3):pass
                event('callback_ready',port=redirect.port)
                request_meta={'requested_scopes':parse_qs(parsed.query).get('scope',[''])[0].split(),
                              'authorization_method':'claude_setup_token',
                              'expected_email':config['expected_email'],
                              'actual_expiry_known':False}
                (state/'authorization-metadata.private.json').write_text(json.dumps(request_meta))
                private_url=state/'authorization-url.private'
                private_url.write_text(url);private_url.chmod(0o600)
                if args.open_browser:
                    cmd=['/usr/bin/open',url] if sys.platform=='darwin' else ['xdg-open',url]
                    opener=subprocess.run(cmd,capture_output=True,timeout=15)
                    if opener.returncode:raise RuntimeError('browser_open_failed')
                opened=True
                event('waiting_for_user',port=redirect.port)
            clean=ansi.sub('',captured.decode('utf-8','replace'))
            match=re.search(r'(sk-ant-oat\d+-[A-Za-z0-9_-]{50,})(?=[^A-Za-z0-9_-])',clean)
            if match:
                credential={'type':'claude','access_token':match.group(1),
                            'refresh_token':'','is_setup_token':True,
                            'requested_scopes':request_meta['requested_scopes'],
                            'authorization_method':'claude_setup_token'}
                local=work/'credential.private.json'
                local.write_text(json.dumps(credential));local.chmod(0o600)
                event('received_credential')
                result=remote(config,'import',credential)
                imported=bool(result.get('imported'))
                usable=result.get('generation_status')==200 and bool(result.get('has_text'))
                phase='completed' if result.get('enabled') else (
                    'pending_identity_and_quota_authorization' if usable else 'imported_validation_failed')
                event(phase,**result)
                return
            if len(captured)>2*1024*1024:raise RuntimeError('login_output_limit')
        raise RuntimeError('login_timed_out_restart_required')
    finally:
        try:os.kill(pid,signal.SIGTERM)
        except ProcessLookupError:pass
        os.close(fd)
        (state/'authorization-url.private').unlink(missing_ok=True)
        if imported or not (work/'credential.private.json').exists():
            shutil.rmtree(work)
        else:
            event('credential_saved_import_pending',private_file=str(work/'credential.private.json'))


if __name__=='__main__':
    try:main()
    except Exception as error:
        # Exception details can include HTTP URLs or private provider responses.
        print(json.dumps({'phase':'failed','reason':str(error) if type(error) is RuntimeError else type(error).__name__}),flush=True)
        sys.exit(1)

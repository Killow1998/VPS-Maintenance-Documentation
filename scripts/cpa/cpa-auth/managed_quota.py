#!/usr/bin/env python3
"""Supervise a separate, identity-checked CPA OAuth grant for quota lookup."""
import argparse
import json
import os
from pathlib import Path
import select
import shlex
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

REMOTE = r'''
import json,os,sys,subprocess,tempfile,shutil,select,time,re,socket,signal
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler
from urllib.parse import quote
j=json.load(sys.stdin)
def stop_session(*_):raise SystemExit(130)
signal.signal(signal.SIGTERM,stop_session)
signal.signal(signal.SIGHUP,stop_session)
oc=json.loads(Path(j['observer_config']).read_text())
op=build_opener(ProxyHandler({}))
def emit(phase,**extra):
    print(json.dumps({'phase':phase,**extra}),flush=True)
def call(path,data=None,method=None):
    req=Request(oc['cpa_base_url'].rstrip('/')+path,headers={
        'Authorization':'Bearer '+oc['management_key'],'Content-Type':'application/json'},
        data=json.dumps(data).encode() if data is not None else None,method=method)
    with op.open(req,timeout=50) as r:return json.loads(r.read(4*1024*1024))
try:
    cfg=call('/v8/management/config')
    files=call('/v8/management/credentials').get('files',[])
    main=next(f for f in files if f.get('name')==j['credential_name'])
    if any(f.get('name')==j['quota_credential_name'] for f in files):
        raise RuntimeError('quota_credential_already_exists')
    authdir=Path(os.path.expanduser(cfg['oauth']['auth-dir']))
    main_data=json.loads((authdir/j['credential_name']).read_text())
    if main_data.get('is_setup_token') is not True:raise RuntimeError('main_credential_not_setup_token')
    # Preserve the token but keep a quota-incomplete account out of the accepted pool.
    call('/v8/management/credentials/status',{'name':j['credential_name'],'disabled':True},'PATCH')
    for f in files:
        if f.get('provider')=='claude':
            p=authdir/f['name']
            if p.is_file():os.chmod(p,0o600)
    pid=subprocess.check_output(['systemctl','show','cliproxyapi','--property=MainPID','--value'],text=True).strip()
    binary=os.readlink('/proc/'+pid+'/exe')
    work=Path(tempfile.mkdtemp(prefix='cpa-quota-login-'))
    proc=None
    try:
        os.chmod(work,0o700);(work/'auth').mkdir(mode=0o700)
        cfg['oauth']['auth-dir']=str(work/'auth')
        cfg['management']={'secret-key':'','allow-remote':False,'disable-control-panel':True}
        cfg['observability']={'logs':{'debug':False,'logging-to-file':False,'request-log':False}}
        p=work/'config.yaml';p.write_text(json.dumps(cfg));p.chmod(0o600)
        proc=subprocess.Popen([binary,'--config',str(p),'--claude-login','--no-browser'],cwd=work,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        output=bytearray();sent=False;deadline=time.monotonic()+330
        while time.monotonic()<deadline:
            if select.select([proc.stdout],[],[],.25)[0]:
                chunk=os.read(proc.stdout.fileno(),65536)
                if chunk:output.extend(chunk)
            if not sent:
                match=re.search(rb'https://claude\.ai/oauth/authorize[^\s\x1b]+',output)
                if match:
                    with socket.create_connection(('127.0.0.1',54545),timeout=3):pass
                    emit('authorization_ready',authorization_url=match.group().decode())
                    sent=True
            if proc.poll() is not None:break
            if len(output)>1024*1024:raise RuntimeError('login_output_limit')
        if proc.poll() is None:raise RuntimeError('quota_login_timed_out')
        candidates=list((work/'auth').glob('*.json'))
        if len(candidates)!=1:raise RuntimeError('quota_credential_not_received')
        credential=json.loads(candidates[0].read_text())
        if str(credential.get('email','')).lower()!=j['expected_email'].lower():
            raise RuntimeError('authorized_account_does_not_match_expected_email')
        if not credential.get('account_uuid') or not credential.get('refresh_token'):
            raise RuntimeError('quota_identity_or_refresh_missing')
        credential.update(disabled=True,cpa_auth_role='quota_only',quota_for=j['credential_name'],
                          authorization_method='cpa_oauth_for_quota',
                          requested_scopes=['user:profile','user:inference','user:sessions:claude_code','user:mcp_servers','user:file_upload'])
        call('/v8/management/credentials?name='+quote(j['quota_credential_name']),credential,'POST')
        (authdir/j['quota_credential_name']).chmod(0o600)
        files=call('/v8/management/credentials').get('files',[])
        source=next(f for f in files if f.get('name')==j['quota_credential_name'])
        def upstream(path):
            return call('/v8/management/requests/api-call',{'auth_index':source['auth_index'],'method':'GET',
                'url':'https://api.anthropic.com/api/oauth/'+path,'header':{
                    'Authorization':'Bearer $TOKEN$','anthropic-beta':'oauth-2025-04-20','Accept':'application/json'}})
        profile=upstream('profile')
        raw=profile.get('body',{});body=json.loads(raw) if isinstance(raw,str) else raw
        account=body.get('account',{}) if isinstance(body,dict) else {}
        verified=profile.get('status_code')==200 and str(account.get('email','')).lower()==j['expected_email'].lower()
        quota=upstream('usage')
        raw=quota.get('body',{});body=json.loads(raw) if isinstance(raw,str) else raw
        windows=quota.get('status_code')==200 and any(isinstance(body.get(w),dict) for w in ('five_hour','seven_day'))
        emit('quota_credential_saved',identity_verified=verified,profile_status=profile.get('status_code'),
             quota_status=quota.get('status_code'),quota_windows_present=windows,
             ready_for_binding=verified and windows)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        shutil.rmtree(work)
except Exception as error:
    emit('failed',reason=str(error) if type(error) is RuntimeError else type(error).__name__)
    sys.exit(1)
'''


def stop_session(*_):
    raise KeyboardInterrupt()


def main():
    signal.signal(signal.SIGTERM, stop_session)
    parser=argparse.ArgumentParser()
    parser.add_argument('--deployment-config',required=True)
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--open-browser',action='store_true',help='Only when the user explicitly asks for automatic opening')
    args=parser.parse_args()
    os.umask(0o077)
    config=json.loads(Path(args.deployment_config).read_text())
    for key in ('expected_email','quota_credential_name','credential_name'):
        if not config.get(key):raise RuntimeError('missing_'+key)
    state=Path(args.state_dir);state.mkdir(parents=True,exist_ok=True,mode=0o700)
    def event(phase,**extra):
        status={'phase':phase,'updated_at':time.time(),**extra}
        p=state/'status.tmp';p.write_text(json.dumps(status));os.replace(p,state/'status.json')
        print(json.dumps(status),flush=True)
    ssh=config['ssh']
    command=ssh[:-1]+['-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=15',
        '-o','ServerAliveCountMax=3','-L','127.0.0.1:54545:127.0.0.1:54545',ssh[-1],
        shlex.join(config.get('remote_python',['python3']))+' -c '+shlex.quote(REMOTE)]
    proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    proc.stdin.write(json.dumps(config).encode());proc.stdin.close()
    event('starting_quota_authorization')
    data=bytearray();opened=False
    try:
        while True:
            if select.select([proc.stdout],[],[],.5)[0]:
                chunk=os.read(proc.stdout.fileno(),65536)
                if chunk:data.extend(chunk)
            while b'\n' in data:
                line,_,data=data.partition(b'\n')
                try:message=json.loads(line)
                except (ValueError,TypeError):continue
                if message.get('phase')=='authorization_ready':
                    url=message.pop('authorization_url')
                    parsed=urlsplit(url);query=parse_qs(parsed.query)
                    if parsed.scheme!='https' or parsed.hostname!='claude.ai':
                        raise RuntimeError('unexpected_authorization_host')
                    redirect=urlsplit(query.get('redirect_uri',[''])[0])
                    if redirect.hostname!='localhost' or redirect.port!=54545 or redirect.scheme!='http':
                        raise RuntimeError('unexpected_callback')
                    if 'user:profile' not in query.get('scope',[''])[0].split():
                        raise RuntimeError('profile_scope_not_requested')
                    with socket.create_connection(('127.0.0.1',54545),timeout=3):pass
                    query['login_hint']=[config['expected_email']]
                    url=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(query,doseq=True),''))
                    private_url=state/'authorization-url.private'
                    private_url.write_text(url);private_url.chmod(0o600)
                    if args.open_browser:
                        result=subprocess.run(['/usr/bin/open',url] if sys.platform=='darwin' else ['xdg-open',url],
                            capture_output=True,timeout=15)
                        if result.returncode:raise RuntimeError('browser_open_failed')
                    opened=True;event('waiting_for_user',callback_port=54545)
                else:
                    event(message.pop('phase','unknown'),**message)
            if proc.poll() is not None:
                if proc.returncode:raise RuntimeError('quota_authorization_failed')
                if not opened:raise RuntimeError('authorization_page_not_opened')
                return
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=6)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        (state/'authorization-url.private').unlink(missing_ok=True)


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'phase':'failed','reason':str(error) if type(error) is RuntimeError else type(error).__name__}),flush=True)
        sys.exit(1)

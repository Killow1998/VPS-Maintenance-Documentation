"""Initialize only disposable data, test local HTTP/auth, and never commit the test container."""

import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request


os.umask(0o077)
image = sys.argv[1]
destination = pathlib.Path(sys.argv[2]).resolve()
inputs = json.loads((pathlib.Path(__file__).parent / 'inputs.json').read_text())
root = pathlib.Path(tempfile.mkdtemp(prefix='xboard-clean-smoke.'))
name = 'xboard-clean-smoke-' + secrets.token_hex(5)
env = root / 'app.env'
env.touch()
for directory in ['database', 'redis', 'logs', 'theme', 'plugins']:
    (root / directory).mkdir(mode=0o755)
    # umask(077) also affects mkdir's requested mode; PHP must traverse the Redis volume.
    (root / directory).chmod(0o755)
mounts = []
for source, target in [('app.env', '/www/.env'), ('database', '/www/.docker/.data'),
                       ('redis', '/data'), ('logs', '/www/storage/logs'),
                       ('theme', '/www/storage/theme'), ('plugins', '/www/plugins')]:
    mounts += ['--mount', 'type=bind,src=' + str(root / source) + ',dst=' + target]
base = ['docker', 'run', '--memory=512m', '--memory-swap=768m', '--cpus=1', '--pids-limit=256']
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
report = {}


def execute(*args, input=None):
    return subprocess.check_output(['docker', 'exec', '-i', name, *args], input=input, stderr=subprocess.STDOUT)


try:
    initialized = subprocess.run(base + ['--rm', '--network=none'] + mounts + [
        '-e', 'ENABLE_SQLITE=true', '-e', 'ENABLE_REDIS=true',
        '-e', 'ADMIN_ACCOUNT=smoke@example.invalid', image, 'php', 'artisan', 'xboard:install', '--no-interaction'],
        capture_output=True, timeout=180)
    assert initialized.returncode == 0, 'Fresh installation exited with error'
    # Check the disposable DB inside Docker: hosted runners need not share the app's UID 1000.
    checked = subprocess.check_output(base + ['--rm', '--network=none', '--entrypoint', 'php'] + mounts + [image, '-r',
        '$p=new PDO("sqlite:/www/.docker/.data/database.sqlite");echo json_encode(["integrity"=>$p->query("PRAGMA quick_check")->fetchAll(PDO::FETCH_COLUMN),"users"=>$p->query("SELECT count(*) FROM v2_user")->fetchColumn()]);'])
    checked = json.loads(checked)
    assert checked['integrity'] == ['ok'] and int(checked['users']) == 1
    subprocess.run(base + ['-d', '--name', name, '-p', '127.0.0.1::7001'] + mounts + [
        '-e', 'RESOURCE_PROFILE=minimal', image], check=True, stdout=subprocess.DEVNULL)
    info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
    port = info['NetworkSettings']['Ports']['7001/tcp'][0]['HostPort']
    local = 'http://127.0.0.1:' + port
    started = time.monotonic()
    deadline = started + 600
    while time.monotonic() < deadline:
        try:
            with direct.open(local + '/', timeout=1) as response:
                if response.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError('Isolated panel did not become ready')
    ready_seconds = round(time.monotonic() - started, 3)
    runtime = json.loads(execute('php', '-r', 'echo json_encode(["php"=>PHP_VERSION,"swoole"=>phpversion("swoole"),"redis"=>phpversion("redis")]);'))
    assert runtime['php'] == inputs['php_version']
    execute('composer', 'check-platform-reqs', '--no-dev')
    horizon = execute('php', 'artisan', 'horizon:status').decode()
    assert 'running' in horizon.lower()
    password = secrets.token_urlsafe(24)
    script = '''require "/www/vendor/autoload.php";$a=require "/www/bootstrap/app.php";$a->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap();$u=App\\Models\\User::firstOrFail();$u->password=password_hash(stream_get_contents(STDIN),PASSWORD_DEFAULT);$u->transfer_enable=1073741824;$u->expired_at=time()+86400;$u->save();echo json_encode(["token"=>$u->token,"admin_path"=>admin_setting("secure_path",admin_setting("frontend_admin_path",hash("crc32b",config("app.key"))))]);'''
    account = json.loads(execute('php', '-r', script, input=password.encode()))
    with direct.open(local + '/' + account['admin_path'], timeout=10) as response:
        assert response.status == 200
        html = response.read().decode()
    assets = re.findall(r'''(?:src|href)=["'](/assets/admin/[^"']+)["']''', html)
    assert assets, 'Admin bundle references missing'
    for asset in assets[:3]:
        with direct.open(local + asset, timeout=10) as response:
            assert response.status == 200 and response.read(128)
    login = urllib.request.Request(local + '/api/v1/passport/auth/login',
        data=json.dumps({'email': 'smoke@example.invalid', 'password': password}).encode(),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    with direct.open(login, timeout=10) as response:
        assert response.status == 200 and json.loads(response.read()).get('data')
    subscribe = urllib.request.Request(local + '/api/v1/client/subscribe?' + urllib.parse.urlencode({'token': account['token']}),
        headers={'User-Agent': 'ClashMeta/1.19.32'})
    with direct.open(subscribe, timeout=10) as response:
        assert response.status == 200
        content = response.read()
    summary = json.loads(execute('php', '-r', '''require "/www/vendor/autoload.php";$c=Symfony\\Component\\Yaml\\Yaml::parse(stream_get_contents(STDIN));echo json_encode(["nodes"=>count($c["proxies"]),"rules"=>count($c["rules"])]);''', input=content))
    assert summary['rules'] > 0
    report = {'result': 'passed', 'runtime': runtime, 'fresh_install': True,
              'image_id': info['Image'],
              'sqlite_integrity': 'ok', 'initial_admin_count': 1, 'panel_http': 200,
              'login_http': 200, 'subscription_http': 200, 'subscription': summary,
              'admin_page_http': 200, 'admin_assets_tested': min(3, len(assets)),
              'startup_seconds': ready_seconds,
              'horizon_running': True, 'production_data_used': False,
              'scope': 'panel image only; no proxy node or production forwarding test'}
finally:
    subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
    # The entrypoint may have changed ownership, so remove only this exact test tree as container root.
    subprocess.run(['docker', 'run', '--rm', '--network=none', '--entrypoint', '/bin/sh',
                    '--mount', 'type=bind,src=' + str(root) + ',dst=/smoke', image,
                    '-c', 'find /smoke -mindepth 1 -delete'], capture_output=True, check=True)
    root.rmdir()
    report['temporary_data_removed'] = True
    destination.write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)

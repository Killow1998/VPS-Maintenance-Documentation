"""Build a clean Linux/amd64 image in a dedicated CPU/memory-limited BuildKit."""

import json
import collections
import pathlib
import secrets
import shutil
import subprocess
import sys


recipe = pathlib.Path(__file__).resolve().parent
inputs = json.loads((recipe / 'inputs.json').read_text())
root = pathlib.Path(sys.argv[1]).resolve()
image = sys.argv[2] if len(sys.argv) > 2 else 'local/xboard-clean:' + inputs['image_tag']
assert root.is_dir() and not (root / 'prepared').exists(), 'Use a fresh, empty work directory'
assert shutil.disk_usage(root).free > 12 * 1024**3, 'Keep at least 12 GiB available before building'
subprocess.run([sys.executable, str(recipe / 'prepare.py'), str(root / 'prepared')], check=True)
builder = 'xboard-clean-' + secrets.token_hex(5)
subprocess.run(['docker', 'buildx', 'create', '--name', builder, '--driver', 'docker-container',
                '--driver-opt', 'memory=4g,memory-swap=5g,cpu-period=100000,cpu-quota=200000,restart-policy=no',
                '--buildkitd-config', str(recipe / 'buildkitd.toml')], check=True)
try:
    command = ['docker', 'buildx', 'build', '--builder', builder, '--platform', inputs['platform'],
               '--progress', 'plain', '--provenance=false', '--load', '--tag', image]
    for name, field in [('PHP_IMAGE', 'php_image'), ('COMPOSER_IMAGE', 'composer_image'),
                        ('EXTENSION_INSTALLER_IMAGE', 'extension_installer_image'), ('UPSTREAM_COMMIT', 'upstream_commit')]:
        command += ['--build-arg', name + '=' + inputs[field]]
    command += [str(root / 'prepared/context')]
    with (root / 'build.log').open('wb') as log:
        completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if completed.returncode:
        print(json.dumps({'result': 'build_failed', 'log': str(root / 'build.log')}), flush=True)
        with (root / 'build.log').open(errors='replace') as log:
            print(''.join(collections.deque(log, maxlen=25))[-12000:], flush=True)
        raise SystemExit(completed.returncode)
    record = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
    report = {'image': image, 'image_id': record['Id'], 'inputs': inputs, 'platform': inputs['platform'],
              'resource_limits': {'cpus': 2, 'memory_gib': 4, 'memory_plus_swap_gib': 5, 'compile_jobs': 2}}
    (root / 'build-result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
finally:
    # Only this dedicated builder and its cache; never prune shared Docker state.
    subprocess.run(['docker', 'buildx', 'rm', builder], check=True)

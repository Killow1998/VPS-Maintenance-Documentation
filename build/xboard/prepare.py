"""Create an allowlisted build context exclusively from locked public Git sources."""

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys


recipe = pathlib.Path(__file__).resolve().parent
inputs = json.loads((recipe / 'inputs.json').read_text())
root = pathlib.Path(sys.argv[1]).resolve()
root.mkdir(mode=0o700)
checkout = root / 'checkout'
subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
subprocess.run(['git', '-C', str(checkout), 'remote', 'add', 'origin', inputs['upstream_url']], check=True)
subprocess.run(['git', '-C', str(checkout), 'fetch', '-q', '--depth=1', 'origin', inputs['upstream_commit']], check=True)
subprocess.run(['git', '-C', str(checkout), 'checkout', '-q', '--detach', 'FETCH_HEAD'], check=True)
subprocess.run(['git', '-C', str(checkout), 'submodule', 'update', '--init', '--recursive', '--depth=1'], check=True)
admin = checkout / 'public/assets/admin'
assert subprocess.check_output(['git', '-C', str(admin), 'rev-parse', 'HEAD']).decode().strip() == inputs['admin_commit']
context = root / 'context'
source = context / 'source'
source.mkdir(parents=True)
for repository, prefix in [(checkout, pathlib.Path()), (admin, pathlib.Path('public/assets/admin'))]:
    names = subprocess.check_output(['git', '-C', str(repository), 'ls-files', '-z']).decode().split('\0')
    for name in filter(None, names):
        path = repository / name
        if path.is_dir():
            continue
        assert not path.is_symlink(), 'Review upstream symlink before including it'
        assert path.resolve().is_relative_to(repository.resolve())
        relative = prefix / name
        assert relative.name not in {'.env', 'auth.json', 'database.sqlite'}, 'Unexpected private-data path'
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
example = source / '.env.example'
lines = example.read_text().splitlines()
example.write_text('\n'.join('APP_KEY=' if line.startswith('APP_KEY=') else line for line in lines) + '\n')
assert not (source / '.env').exists()
assert not list(source.rglob('.git'))
shutil.copy2(recipe / 'Dockerfile', context / 'Dockerfile')
files = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in sorted(source.rglob('*')) if p.is_file()}
report = {'inputs': inputs, 'files': files, 'public_example_app_key_cleared': True,
          'production_inputs_used': False, 'recipe_sha256': hashlib.sha256((recipe / 'Dockerfile').read_bytes()).hexdigest()}
(root / 'provenance.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'context_ready': True, 'source_commit': inputs['upstream_commit'], 'files': len(files)}))

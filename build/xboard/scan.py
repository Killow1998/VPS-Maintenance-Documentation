"""Conservative publish gate over image metadata and every readable archive layer."""

import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile


image, directory = sys.argv[1:]
root = pathlib.Path(directory).resolve()
archive = root / 'image.tar'
subprocess.run(['docker', 'image', 'save', '-o', str(archive), image], check=True)
info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
assert not any(k.startswith('com.docker.compose.') for k in info['Config'].get('Labels', {}))
assert info['Architecture'] == 'amd64' and info['Os'] == 'linux'
patterns = [rb'gh[pousr]_[A-Za-z0-9]{36,}', rb'github_pat_[A-Za-z0-9_]{30,}',
            rb'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{40,}-----END (?:OPENSSH |RSA |EC )?PRIVATE KEY-----']
findings = []
layers = 0
checked = 0


def inspect_bytes(name, content):
    global checked
    checked += 1
    if any(re.search(pattern, content) for pattern in patterns):
        findings.append({'path': name, 'reason': 'credential-pattern-review-required'})
    clean = name.removeprefix('./').lstrip('/')
    if clean == 'www/.env' or clean.startswith('www/.env.backup'):
        findings.append({'path': clean, 'reason': 'application-environment-file'})
    if clean.startswith('www/') and (clean.endswith('.sqlite') or clean.endswith('.rdb')):
        findings.append({'path': clean, 'reason': 'application-database-file'})
    if clean.startswith('www/storage/logs/') and not clean.endswith('.gitignore') and content:
        findings.append({'path': clean, 'reason': 'nonempty-application-log'})
    if clean == 'www/.env.example' and re.search(rb'^APP_KEY=\S+', content, re.M):
        findings.append({'path': clean, 'reason': 'nonempty-example-app-key'})


inspect_bytes('image-config', json.dumps(info['Config']).encode())
with tarfile.open(archive) as outer:
    for entry in outer:
        if not entry.isfile() or not (entry.name.endswith('/layer.tar') or entry.name.startswith('blobs/sha256/')):
            continue
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as blob:
            stream = outer.extractfile(entry)
            while chunk := stream.read(8 * 1024 * 1024):
                blob.write(chunk)
            blob.seek(0)
            try:
                layer = tarfile.open(fileobj=blob, mode='r:*')
            except tarfile.ReadError:
                continue
            layers += 1
            with layer:
                for member in layer:
                    if member.isfile():
                        content = layer.extractfile(member).read(2 * 1024 * 1024)
                        inspect_bytes(member.name, content)
report = {'result': 'passed' if not findings else 'review_required', 'image_id': info['Id'],
          'layers_checked': layers, 'file_prefixes_checked': checked, 'findings': findings,
          'scope': 'metadata and up to 2 MiB per regular layer file; not a complete malware/vulnerability audit'}
assert layers > 0
(root / 'scan.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'result': report['result'], 'layers_checked': layers, 'files_checked': checked,
                  'finding_count': len(findings)}), flush=True)
assert not findings, 'Review private scan report before publishing; do not bypass'

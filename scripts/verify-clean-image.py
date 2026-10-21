"""Verify a previously checksum-checked Release archive across Docker image stores."""

import hashlib
import json
import pathlib
import subprocess
import sys
import tarfile


archive, manifest = map(pathlib.Path, sys.argv[1:])
release = json.loads(manifest.read_text())
image = release['build']['image']
with tarfile.open(archive) as saved:
    entries = json.load(saved.extractfile('manifest.json'))
    assert len(entries) == 1 and entries[0]['RepoTags'] == [image]
    config_bytes = saved.extractfile(entries[0]['Config']).read()
    config_digest = 'sha256:' + hashlib.sha256(config_bytes).hexdigest()
    config = json.loads(config_bytes)
    identities = {config_digest}
    if 'index.json' in saved.getnames():
        index = json.load(saved.extractfile('index.json'))
        for descriptor in index.get('manifests', []):
            digest = descriptor['digest']
            blob = saved.extractfile('blobs/' + digest.replace(':', '/')).read()
            assert 'sha256:' + hashlib.sha256(blob).hexdigest() == digest
            descriptor_manifest = json.loads(blob)
            assert descriptor_manifest['config']['digest'] == config_digest
            identities.add(digest)
    assert release['build']['image_id'] in identities
subprocess.run(['docker', 'image', 'load', '-i', str(archive)], check=True, stdout=subprocess.DEVNULL)
actual = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
assert actual['Id'] in identities, 'Loaded image identity is not part of the verified archive'
if actual.get('Descriptor'):
    assert actual['Descriptor']['digest'] in identities
assert actual['RootFS']['Layers'] == config['rootfs']['diff_ids']
for key in ['Env', 'Entrypoint', 'Cmd', 'WorkingDir', 'User', 'ExposedPorts', 'Labels']:
    assert (actual['Config'].get(key) or None) == (config['config'].get(key) or None), 'Runtime config differs: ' + key
runtime = json.loads(subprocess.check_output(['docker', 'run', '--rm', '--network=none', '--memory=256m', '--cpus=1',
    '--entrypoint', 'php', image, '-r',
    'echo json_encode(["php"=>PHP_VERSION,"swoole"=>phpversion("swoole"),"env_present"=>file_exists("/www/.env"),"database_present"=>file_exists("/www/.docker/.data/database.sqlite")]);']))
assert runtime['php'] == release['build']['inputs']['php_version']
assert not runtime['env_present'] and not runtime['database_present']
print(json.dumps({'build_image_id': release['build']['image_id'], 'loaded_image_id': actual['Id'],
                  'config_digest': config_digest, 'rootfs_and_runtime_config_verified': True, 'runtime': runtime}))

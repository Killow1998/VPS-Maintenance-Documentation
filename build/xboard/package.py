"""Package only the scanned immutable image and public corresponding source."""

import gzip
import hashlib
import json
import pathlib
import shutil
import sys
import tarfile


root = pathlib.Path(sys.argv[1]).resolve()
build = json.loads((root / 'build-result.json').read_text())
scan = json.loads((root / 'scan.json').read_text())
smoke = json.loads((root / 'smoke.json').read_text())
assert scan['result'] == smoke['result'] == 'passed'
assert scan['image_id'] == smoke['image_id'] == build['image_id'] and smoke['temporary_data_removed']
release = root / 'release'
release.mkdir()
with (root / 'image.tar').open('rb') as source, gzip.open(release / 'xboard-clean-linux-amd64.tar.gz', 'wb', compresslevel=3) as output:
    shutil.copyfileobj(source, output, 8 * 1024 * 1024)
with tarfile.open(release / 'xboard-upstream-source.tar.gz', 'w:gz') as source:
    source.add(root / 'prepared/context/source', arcname='Xboard')
shutil.copy2(root / 'prepared/provenance.json', release / 'provenance.json')
manifest = {'build': build, 'smoke': smoke, 'scan': scan,
            'production_data_used': False, 'initial_accounts_in_image': 0}
(release / 'manifest.json').write_text(json.dumps(manifest, indent=2))
lines = []
for path in sorted(release.iterdir()):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    lines.append(h.hexdigest() + '  ' + path.name)
(release / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')
print(json.dumps({'release_ready': True, 'files': [p.name for p in sorted(release.iterdir())]}), flush=True)

# Transferring Cagliari corrections and impedances

Cagliari's manual POI inputs are tracked in Git:

- `tools/gi_bi_relabeling/relabels.json`
- `tools/gi_bi_relabeling/manual_merges.json`

Commit and push future edits to these files; pull them on the other computer.
POI definitions in `config/poi_types.csv` and `config/services.csv` are also tracked.
Generated tool exports and review approvals remain ignored. Paris corrections
and bundles are not included in this handoff.

The large routing bundle remains outside Git history. Download it from the
[GitHub Release](https://github.com/davyatze01/CapabilityModel/releases/tag/impedance-bundles-2026-11-25).
After cloning/pulling the repository, run from its root with GitHub CLI installed:

```sh
mkdir -p artifacts/Cagliari
gh release download impedance-bundles-2026-11-25 \
  --repo davyatze01/CapabilityModel \
  --pattern Cagliari_impedances.npz \
  --output artifacts/Cagliari/impedances.npz
```

Verify it with Python (no additional packages needed):

```sh
python - <<'PY'
import hashlib
import json
from pathlib import Path

metadata = json.loads(Path('config/cagliari_impedance_bundle.json').read_text())
path = Path(metadata['restore_path'])
with path.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
if path.stat().st_size != metadata['size_bytes'] or digest != metadata['sha256']:
    raise SystemExit('Bundle verification failed')
print('Cagliari bundle verified')
PY
```

The metadata identifies this exact saved routing snapshot (bundle schema 4,
non-bus cache schema 13). Its checksum proves file integrity, not compatibility
with future changes to POIs, routing parameters, or model code. Rebuild affected
impedances after such changes and publish a new version rather than overwriting
this release. The current bundle does not record the POI configuration hashes
from its build, so this release cannot certify that provenance retrospectively.

This download supplies the main Cagliari bundle only. It does not include persona
bundles, graphs, POI datasets, or software dependencies; existing scenario input
requirements still apply.

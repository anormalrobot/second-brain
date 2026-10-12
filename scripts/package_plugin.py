#!/usr/bin/env python3
"""Build a clean local marketplace for both clients, excluding all personal data."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

try:
    from . import brain, second_brain as setup
except ImportError:
    import brain
    import second_brain as setup


def build(destination):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Package destination must be new; existing releases are preserved')
    files = setup.runtime_files()  # Explicit allowlist; no vault, identity, registry, or test data.
    metadata = json.loads((setup.SOURCE / 'packaging/plugin.json').read_text())
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.brain-package-', dir=destination.parent))
    try:
        plugin = staging / 'plugins/second-brain'
        setup.stage_runtime(plugin, files)
        # Native manifests are the entry points for both supported clients.
        # A portable root manifest shadows Codex's native hook configuration in 0.162.1.
        common = {k: metadata[k] for k in ('name', 'version', 'description')}
        brain.save_json(plugin / '.claude-plugin/plugin.json', common)
        brain.save_json(plugin / '.codex-plugin/plugin.json', {**common, 'skills': './skills/', 'hooks': './hooks/hooks.json'})
        brain.save_json(staging / '.agents/plugins/marketplace.json', {
            'name': 'second-brain-local', 'interface': {'displayName': metadata['name']},
            'plugins': [{'name': 'second-brain', 'source': {'source': 'local', 'path': './plugins/second-brain'},
                         'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_USE'}, 'category': 'Productivity'}]})
        brain.save_json(staging / '.claude-plugin/marketplace.json', {
            'name': 'second-brain-local', 'owner': {'name': metadata['name']},
            'plugins': [{'name': 'second-brain', 'source': './plugins/second-brain', 'description': common['description']}]})
        inventory = {p.relative_to(staging).as_posix(): brain.digest(p.read_bytes())
                     for p in sorted(staging.rglob('*')) if p.is_file()}
        brain.save_json(staging / 'package-manifest.json', {'version': metadata['version'], 'files': inventory})
        if destination.exists():
            raise ValueError('Package destination appeared during build')
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {'marketplace': str(destination), 'plugin': str(destination / 'plugins/second-brain'),
            'version': metadata['version'], 'files': len(inventory)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.output), indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

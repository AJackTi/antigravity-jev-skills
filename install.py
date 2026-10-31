#!/usr/bin/env python3
"""Verify this three-skill bundle, optionally register user-owned symlinks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

PACKAGES = {
    'antigravity-task-runner':'antigravity-task-runner',
    'antigravity-sol-quant-runner':'antigravity-sol-quant-runner',
    'jev':'jev-calibrated-decisions',
}


def verify(bundle):
    manifest=json.loads((bundle/'manifest.json').read_text())
    for name,entry in manifest['files'].items():
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('unsafe manifest path')
        file=bundle/relative
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(bundle):
            raise ValueError('missing or unsafe packaged file: '+name)
        data=file.read_bytes()
        if len(data)!=entry['bytes'] or hashlib.sha256(data).hexdigest()!=entry['sha256']:
            raise ValueError('file hash mismatch: '+name)
    for directory in PACKAGES.values():
        if not (bundle/directory/'SKILL.md').is_file():raise ValueError('missing skill entrypoint')
    return len(manifest['files'])


def main():
    if sys.version_info<(3,10):raise SystemExit('Python 3.10+ required')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install',action='store_true',help='register the three skills and two CLI launchers')
    parser.add_argument('--replace-links',action='store_true',help='replace only owned symlinks; never regular files/directories')
    parser.add_argument('--target-root',type=Path,default=Path.home(),help='user directory (default current user); useful for isolated verification')
    args=parser.parse_args();bundle=Path(__file__).resolve().parent
    try:
        count=verify(bundle)
        print(f'Integrity PASS: {count} packaged files')
        if not args.install:
            print('Verification only. Use --install to register skills; credentials are external.')
            return 0
        target_root=args.target_root.expanduser().resolve()
        targets=[(target_root/'.agents/skills'/name,bundle/directory) for name,directory in PACKAGES.items()]
        targets.extend((target_root/'.local/bin'/name,bundle/'jev-calibrated-decisions/scripts'/name) for name in ('jev','jev-pool'))
        for destination,source in targets:
            if destination.is_symlink():
                if destination.resolve()==source.resolve():continue
                if not args.replace_links or destination.lstat().st_uid!=os.getuid():
                    raise ValueError('existing link preserved: '+str(destination))
            elif destination.exists():
                raise ValueError('existing file/directory preserved: '+str(destination))
        # Validate all collisions before creating any registration.
        for name in ('jev','jev-pool'):(bundle/'jev-calibrated-decisions/scripts'/name).chmod(0o755)
        for destination,source in targets:
            destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.is_symlink():
                if destination.resolve()==source.resolve():continue
                destination.unlink()
            destination.symlink_to(source,target_is_directory=source.is_dir())
        print('Registered 3 skills and jev/jev-pool. Keep this extracted folder in place.')
        print('Add the target .local/bin to PATH if needed. Install/authenticate agy separately.')
        print('Configure TypeSafe keys privately on this machine; none are bundled.')
        return 0
    except (OSError,ValueError,KeyError,json.JSONDecodeError) as exc:
        print('Installation stopped: '+str(exc),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())

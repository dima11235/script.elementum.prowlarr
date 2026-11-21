"""Собрать воспроизводимый Kodi ZIP из файлов аддона (порт script.elementum.jackett)."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent
PACKAGE_ROOTS = ('src', 'resources')
PACKAGE_FILES = ('addon.xml', 'LICENSE', 'README.md', 'CHANGELOG.md')
EXCLUDED_PARTS = frozenset({'__pycache__', '.git'})


def package_files(root=ROOT):
    """Every file of the addon tree except caches, sorted for reproducibility."""
    files = [name for name in PACKAGE_FILES if (root / name).is_file()]
    for base in PACKAGE_ROOTS:
        for path in sorted((root / base).rglob('*')):
            if not path.is_file():
                continue
            if any(part in EXCLUDED_PARTS for part in path.parts):
                continue
            relative = path.relative_to(root)
            if relative.parts[:2] == ('resources', 'libs'):
                if path.suffix != '.py' and relative.parts[2] != 'licenses':
                    continue
            if path.suffix in ('.pyc', '.pyo'):
                continue
            files.append(relative.as_posix())
    return sorted(files)


def build(output, root=ROOT):
    manifest = ET.parse(root / 'addon.xml').getroot()
    addon_id, version = manifest.get('id'), manifest.get('version')
    if addon_id != 'script.elementum.prowlarr' or not re.fullmatch(r'\d+\.\d+\.\d+', version or ''):
        raise ValueError('Invalid addon id or version')
    files = package_files(root)
    entrypoint = manifest.find("./extension[@point='xbmc.python.script']").get('library')
    required = {entrypoint, 'resources/settings.xml', 'resources/libs/aiohttp/__init__.py',
                'resources/libs/torf/__init__.py', 'resources/libs/flatbencode.py'}
    if missing := required.difference(files):
        raise ValueError(f"Missing package inputs: {', '.join(sorted(missing))}")
    tag = os.environ.get('GITHUB_REF', '')
    if tag.startswith('refs/tags/') and tag != f'refs/tags/v{version}':
        raise ValueError('Git tag must match addon.xml version')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'{addon_id}-{version}.zip'
    with ZipFile(target, 'w', compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            path = root / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'Missing or symbolic package input: {relative}')
            data = path.read_bytes()
            if path.suffix == '.py':
                compile(data, relative, 'exec')
            entry = ZipInfo(f'{addon_id}/{relative}', (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            mode = 0o100755 if path.suffix == '.so' else 0o100644
            entry.external_attr = mode << 16
            entry.compress_type = ZIP_DEFLATED
            archive.writestr(entry, data, compresslevel=9)
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.zip.sha256').write_text(f'{checksum}  {target.name}\n', encoding='utf-8')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='dist')
    args = parser.parse_args()
    print(build(args.output))

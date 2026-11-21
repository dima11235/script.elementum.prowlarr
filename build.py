"""Собрать воспроизводимый Kodi ZIP только из явного списка файлов."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent
PACKAGE_FILES = (
    'addon.xml', 'addon.py', 'LICENSE', 'README.md', 'CHANGELOG.md',
    'resources/settings.xml', 'resources/lib/__init__.py', 'resources/lib/prowlarr.py',
    'resources/language/resource.language.en_gb/strings.po',
    'resources/language/resource.language.ru_ru/strings.po',
)


def build(output, root=ROOT):
    manifest = ET.parse(root / 'addon.xml').getroot()
    addon_id, version = manifest.get('id'), manifest.get('version')
    if addon_id != 'script.elementum.prowlarr' or not re.fullmatch(r'\d+\.\d+\.\d+', version or ''):
        raise ValueError('Invalid addon id or version')
    tag = os.environ.get('GITHUB_REF', '')
    if tag.startswith('refs/tags/') and tag != f'refs/tags/v{version}':
        raise ValueError('Git tag must match addon.xml version')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'{addon_id}-{version}.zip'
    with ZipFile(target, 'w', compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in sorted(PACKAGE_FILES):
            path = root / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'Missing or symbolic package input: {relative}')
            data = path.read_bytes()
            if path.suffix == '.py':
                compile(data, relative, 'exec')
            entry = ZipInfo(f'{addon_id}/{relative}', (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
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

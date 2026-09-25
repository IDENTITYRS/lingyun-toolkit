"""Build signed update and manual-install archives. Private key stays outside repo."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lingyun_update_core as core


def sign(raw, key):
    n, d = int(key['n'], 16), int(key['d'], 16)
    width = (n.bit_length() + 7) // 8
    hashed = hashlib.sha256(core.DOMAIN + raw).digest()
    encoded = bytes.fromhex('3031300d060960864801650304020105000420') + hashed
    padded = b'\x00\x01' + b'\xff' * (width - len(encoded) - 3) + b'\x00' + encoded
    return base64.b64encode(pow(int.from_bytes(padded, 'big'), d, n).to_bytes(width, 'big'))


def build():
    version = core.current_version(ROOT)
    output = ROOT / 'dist' / ('v' + version)
    output.mkdir(parents=True, exist_ok=True)
    files = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in sorted(ROOT.rglob('*'))
             if p.is_file() and core.code_name(p.relative_to(ROOT).as_posix())}
    archive = output / f'lingyun-toolkit-{version}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr('plugin/' + name, data)
    raw_zip = archive.read_bytes()
    manifest = dict(schema=1, product=core.PRODUCT, version=version,
                    url=f'https://github.com/{core.REPOSITORY}/releases/download/v{version}/{archive.name}',
                    archive_root='plugin', size=len(raw_zip), sha256=core.digest(raw_zip),
                    files={name: core.digest(data) for name, data in files.items()},
                    notes=(ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'))
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    key_path = Path(os.environ['LOCALAPPDATA']) / 'LingyunPublisher/lingyun-toolkit/update-private.json'
    signature = sign(raw, json.loads(key_path.read_text(encoding='utf-8-sig')))
    core.verify_manifest(raw, signature, core.read_json(ROOT / 'lingyun_update_public.json'))
    (output / 'update.json').write_bytes(raw)
    (output / 'update.sig').write_bytes(signature)
    manual = output / f'lingyun-toolkit-{version}-install.zip'
    with zipfile.ZipFile(manual, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(f'凌云·字字实用插件大全·发布版V{version}/' + name, data)
    print(json.dumps(dict(version=version, files=len(files), output=str(output)), ensure_ascii=False))
    return output


if __name__ == '__main__':
    build()

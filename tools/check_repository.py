"""Offline committed-file integrity, syntax and high-confidence private-data checks."""
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'REPOSITORY-MANIFEST.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(root):
    probe = (subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
                            capture_output=True, text=True) if shutil.which('git') else None)
    if probe is not None and probe.returncode == 0 and Path(probe.stdout.strip()).resolve() == root.resolve():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others',
                                 '--exclude-standard'], capture_output=True, check=True)
        names = {value.decode('utf-8') for value in result.stdout.split(b'\0') if value}
    else:
        names = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()
                 and not set(p.relative_to(root).parts) & {'.git', '__pycache__', 'test-results'}}
    return sorted(names - {MANIFEST})


def scan(name, raw):
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
        raise ValueError('invalid repository path: ' + name)
    if set(relative.parts) & {'private', 'secrets', '.lark-cli', 'data', 'wtv081', 'wheelhouse'}:
        raise ValueError('private/runtime file selected: ' + name)
    if relative.suffix.lower() in {'.exe', '.dll', '.whl', '.zip', '.pyc', '.pem', '.key', '.pfx'}:
        raise ValueError('non-source artifact selected: ' + name)
    if relative.name.startswith('.env') and relative.name != '.env.example':
        raise ValueError('environment state selected: ' + name)
    text = raw.decode('utf-8-sig')
    rules = {
        'credential': r'\b(?:glpat-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}',
        'private_key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        'identity': r'\b(?:ou|oc|om)_[a-f0-9]{16,}\b|\bcli_[a-f0-9]{12,}\b',
        'user_path': r'(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s"\x27]+',
        'authenticated_url': r'https?://[^/\s"\x27]+:[^/@\s"\x27]+@',
    }
    for label, pattern in rules.items():
        inspected = text
        if label == 'authenticated_url' and name.endswith('/test_download_scheduler.py'):
            inspected = inspected.replace('https://' + 'user:secret@test.download.invalid/input.bin', '')
        if re.search(pattern, inspected):
            raise ValueError(label + ' found: ' + name)
    # Reject long resource tokens on tenant URLs; explicit synthetic fixtures remain.
    for match in re.finditer(r'https?://([^/\s<>"\x27]+)/(?:sheets|docx|wiki)/([A-Za-z0-9]{20,})', text):
        if match[1] not in {'example.feishu.cn', 'fixture.invalid', 'example.com'}:
            raise ValueError('tenant resource found: ' + name)
    for vin in re.findall(r'(?<![A-Za-z0-9_])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9_])', text):
        if vin not in {'DEM0VEH1CLE000001', 'DEM0VEH1CLE000002', 'DEM0VEH1CLEX00001',
                       'DEM0VEH1CLEX00002', 'DEM0VEH1CLEX00003', 'WBA123456789ABCDE',
                       'WBA00000000000000', 'L1234567890123456', 'L2234567890123456'}:
            # Numeric timings/hashes in manifests are not vehicle identifiers.
            if not vin.isdigit():
                raise ValueError('non-synthetic VIN-shaped value: ' + name)
    if relative.suffix in {'.py', '.pyw'}:
        ast.parse(text, filename=name)


def verify(root=ROOT):
    root = Path(root).resolve()
    manifest = json.loads((root / MANIFEST).read_text('utf-8'))
    if manifest.get('schema') != 'uroad-source-repository-v1':
        raise ValueError('invalid manifest schema')
    actual = files(root)
    expected = manifest.get('files', {})
    if set(actual) != set(expected):
        raise ValueError('repository file set differs from manifest')
    normalized = set()
    for name in actual:
        if name.casefold() in normalized:
            raise ValueError('case collision')
        normalized.add(name.casefold())
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('source is not a plain file: ' + name)
        raw = path.read_bytes()
        if expected[name] != {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}:
            raise ValueError('source bytes differ: ' + name)
        scan(name, raw)
    release = root / 'workspace/.agents/skills/uroad-cloud-report'
    source = json.loads((root / 'SOURCE-MANIFEST.json').read_text('utf-8'))
    actual_source = {p.relative_to(release).as_posix(): sha(p)
                     for p in release.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    if source['files'] != actual_source or source['source_file_count'] != len(actual_source):
        raise ValueError('release manifest differs')
    canonical = hashlib.sha256(json.dumps(actual_source, sort_keys=True, ensure_ascii=False,
                                          separators=(',', ':')).encode()).hexdigest()
    if source['canonical_source_sha256'] != canonical:
        raise ValueError('release canonical digest differs')
    provenance = json.loads((root / 'SOURCE-PROVENANCE.json').read_text('utf-8'))
    for name, expected_digest in provenance['production_source_files'].items():
        if sha(root / name) != expected_digest:
            raise ValueError('production baseline differs from provenance: ' + name)
    return {'status': 'passed', 'files': len(actual), 'release_files': len(actual_source),
            'syntax_and_private_data_checks': 'passed', 'network_requests': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.root)))
    except (ValueError, OSError, SyntaxError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}))
        sys.exit(1)

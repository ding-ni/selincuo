from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get('SELINCO_APP_ROOT', Path(__file__).resolve().parents[1])).resolve()
_active_file = ROOT / 'config/active-workspace.json'
_active = json.loads(_active_file.read_text(encoding='utf-8-sig')) if _active_file.is_file() else {}
WORKSPACE = Path(os.environ.get('SELINCO_WORKSPACE', ROOT / _active.get('workspace', 'workspace'))).resolve()
PROJECT_ID = 'selinco'
CONTEXT_KEY = 'selinco:semidistributed-cold-lake:0p1deg'
VERSION = '0.12.0'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path, default=None):
    p = Path(path)
    return json.loads(p.read_text(encoding='utf-8-sig')) if p.is_file() else default


def write_json(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=p.name + '.', suffix='.tmp', dir=p.parent)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, p)


def within(root, relative):
    base = Path(root).resolve()
    p = (base / relative).resolve()
    if p != base and base not in p.parents:
        raise ValueError('路径超出当前工作区')
    return p


def model_identity():
    files = sorted((ROOT / 'backend').rglob('*.py'))
    return digest({str(p.relative_to(ROOT)).replace('\\', '/'): sha_file(p)
                   for p in files if 'tests' not in p.parts and '__pycache__' not in p.parts})

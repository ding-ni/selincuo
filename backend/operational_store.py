"""SQLite records for connections, forecasts, lake curves and risk products."""
from copy import deepcopy
import json
import sqlite3
import threading
import uuid

from .common import canonical, now

_lock = threading.RLock()


def initialize(db):
    with _lock, db.connect() as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operational_records'").fetchone():
            return
        folder = db.workspace / 'backups' / '业务扩展前备份'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ('hydrology-' + uuid.uuid4().hex[:12] + '.sqlite3')
        with sqlite3.connect(str(path)) as target:
            connection.backup(target)
        connection.execute('''CREATE TABLE operational_records(
            collection TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(collection,id))''')
        connection.execute('CREATE INDEX operational_lookup ON operational_records(collection,updated_at)')


def save(db, collection, value, identifier=None, prefix=''):
    initialize(db)
    result = deepcopy(value)
    identifier = identifier or result.get('id') or prefix + uuid.uuid4().hex[:20]
    if not isinstance(identifier, str) or not identifier:
        raise ValueError('业务记录编号无效')
    stamp = now()
    with db.connect() as connection:
        previous = connection.execute('SELECT created_at FROM operational_records WHERE collection=? AND id=?', (collection, identifier)).fetchone()
        result.update(id=identifier, createdAt=previous[0] if previous else result.get('createdAt', stamp), updatedAt=stamp)
        connection.execute('''INSERT INTO operational_records VALUES(?,?,?,?,?,?)
            ON CONFLICT(collection,id) DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at,payload=excluded.payload''',
            (collection, identifier, str(result.get('name', '')), result['createdAt'], stamp, canonical(result)))
    return result


def get(db, collection, identifier):
    initialize(db)
    rows = db.rows('SELECT payload FROM operational_records WHERE collection=? AND id=?', (collection, identifier))
    if not rows:
        raise FileNotFoundError('业务记录不存在：' + str(identifier))
    return json.loads(rows[0]['payload'])


def list_records(db, collection):
    initialize(db)
    return [json.loads(row['payload']) for row in db.rows('SELECT payload FROM operational_records WHERE collection=? ORDER BY updated_at DESC,id', (collection,))]

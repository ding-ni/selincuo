from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .common import WORKSPACE, canonical, now, read_json, write_json

SCHEMA = '''
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stations(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,lon REAL,lat REAL,metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,imported_at TEXT NOT NULL,metadata TEXT NOT NULL, UNIQUE(path,sha256));
CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),station_id TEXT NOT NULL REFERENCES stations(id),variable TEXT NOT NULL,time TEXT NOT NULL,period_start TEXT,period_end TEXT,step TEXT NOT NULL,unit TEXT NOT NULL,value REAL,raw_text TEXT NOT NULL,quality TEXT NOT NULL,sheet TEXT NOT NULL,cell TEXT NOT NULL, UNIQUE(source_id,sheet,cell,variable));
CREATE INDEX IF NOT EXISTS obs_lookup ON observations(station_id,variable,step,time);
CREATE TABLE IF NOT EXISTS coverage(source_id TEXT NOT NULL,station_id TEXT NOT NULL,variable TEXT NOT NULL,step TEXT NOT NULL,start TEXT,end TEXT,valid INTEGER NOT NULL,total INTEGER NOT NULL,metadata TEXT NOT NULL,PRIMARY KEY(source_id,station_id,variable,step));
CREATE TABLE IF NOT EXISTS input_products(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL,manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parameter_versions(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schemes(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,progress REAL NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT '',request TEXT NOT NULL,result TEXT,trashed INTEGER NOT NULL DEFAULT 0,favorite INTEGER NOT NULL DEFAULT 0,name TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',collection TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS states(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),created_at TEXT NOT NULL,valid_at TEXT NOT NULL,payload TEXT NOT NULL,name TEXT NOT NULL DEFAULT '',favorite INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL);
'''


class Database:
    def __init__(self, workspace=None):
        self.workspace = Path(workspace or WORKSPACE).resolve()
        self.path = self.workspace / 'database' / 'hydrology.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        marker = self.workspace / 'workspace-identity.json'
        identity = read_json(marker)
        if identity and identity.get('projectId') != 'selinco':
            raise ValueError('工作区属于其他项目')
        if self.path.is_file() and not identity:
            raise ValueError('已有数据库缺少色林错工作区身份，拒绝修改；请用完整备份迁移')
        if not identity:
            write_json(marker, {'projectId': 'selinco', 'schemaVersion': 1, 'createdAt': now()})
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            db.execute('INSERT OR IGNORE INTO schema_version VALUES(1,?)', (now(),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=60)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def setting(self, key, default=None):
        rows = self.rows('SELECT value FROM settings WHERE key=?', (key,))
        return json.loads(rows[0]['value']) if rows else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, canonical(value)))

    def log(self, action, payload):
        with self.connect() as db:
            db.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)', (now(), action, canonical(payload)))

    def summary(self):
        with self.connect() as db:
            counts = {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                      for t in ('stations', 'sources', 'observations', 'input_products', 'parameter_versions', 'tasks', 'states')}
            variables = [dict(r) for r in db.execute('SELECT variable,step,COUNT(*) AS records,COUNT(value) AS valid,MIN(time) AS start,MAX(time) AS end FROM observations GROUP BY variable,step')]
            coverage = [dict(r) for r in db.execute('SELECT * FROM coverage ORDER BY station_id,variable,start')]
        for row in coverage:
            row['metadata'] = json.loads(row['metadata'])
        return {'counts': counts, 'variables': variables, 'coverage': coverage}

    def observations(self, station, variable, step=None, start=None, end=None, limit=20000):
        if start and len(start) == 10:
            start += 'T00:00:00'
        if end and len(end) == 10:
            end += 'T23:59:59'
        clauses, args = ['station_id=?', 'variable=?'], [station, variable]
        for field, op, value in [('step', '=', step), ('time', '>=', start), ('time', '<=', end)]:
            if value:
                clauses.append(f'{field}{op}?')
                args.append(value)
        where = ' AND '.join(clauses)
        # New revisions replace the selected view only, never the original source rows.
        sql = f'''SELECT * FROM (SELECT o.*,ROW_NUMBER() OVER(PARTITION BY station_id,variable,step,time ORDER BY s.imported_at DESC,o.id DESC) rn FROM observations o JOIN sources s ON s.id=o.source_id WHERE {where} AND s.rowid IN (SELECT MAX(rowid) FROM sources GROUP BY path)) WHERE rn=1 ORDER BY time LIMIT ?'''
        args.append(min(100000, max(1, int(limit))))
        return self.rows(sql, args)

    def items(self, table):
        if table not in ('input_products', 'parameter_versions', 'schemes', 'states'):
            raise ValueError('未知资料目录')
        rows = self.rows(f'SELECT * FROM {table} ORDER BY created_at DESC')
        for row in rows:
            for key in ('payload', 'manifest'):
                if key in row:
                    row[key] = json.loads(row[key])
        return rows

    def save_item(self, table, item_id, name, payload):
        if table not in ('parameter_versions', 'schemes'):
            raise ValueError('不能写入该目录')
        if not str(name).strip() or len(str(name)) > 120:
            raise ValueError('名称须为1—120个字符')
        with self.connect() as db:
            db.execute(f'INSERT INTO {table} VALUES(?,?,?,?)', (item_id, name.strip(), now(), canonical(payload)))

    def task(self, task_id):
        rows = self.rows('SELECT * FROM tasks WHERE id=?', (task_id,))
        if not rows:
            raise FileNotFoundError('计算记录不存在或已移走')
        row = rows[0]
        row['request'] = json.loads(row['request'])
        row['result'] = json.loads(row['result']) if row['result'] else None
        return row

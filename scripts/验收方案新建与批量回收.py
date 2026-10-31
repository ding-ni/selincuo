"""真实页面与接口验收；写入仅发生在SQLite快照，生产资料只读引用。"""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import urlopen
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import canonical, now, model_identity
from backend.database import Database
from backend.server import Handler
from backend import calibration_configuration
from backend.jobs import Jobs


def get(base, path):
    with urlopen(base + path, timeout=90) as response:
        return json.load(response)


def production_snapshot(workspace):
    with sqlite3.connect('file:' + (workspace / 'database/hydrology.sqlite3').as_posix() + '?mode=ro', uri=True) as conn:
        return {'counts': {name: conn.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                           for name in ('schemes', 'tasks', 'audit', 'settings')},
                'adoption': conn.execute("SELECT value FROM settings WHERE key='business-adopted-scheme-v1'").fetchone()[0],
                'configurations': sorted(path.name for path in (workspace / 'calibration-configurations').glob('*.json'))}


class IsolatedHandler(Handler):
    def post(self):
        path = urlsplit(self.path).path.removeprefix('/api/model/')
        if path not in ('schemes', 'schemes/recycle-preview', 'schemes/recycle', 'schemes/restore-batch'):
            raise ValueError('该隔离验收只允许方案保存与可恢复回收；不创建任务或更改采用')
        super().post()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8882')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    health = get(args.base, '/api/health')
    workspace = Path(health['workspace'])
    before = production_snapshot(workspace)
    db = Database.__new__(Database)
    # Only the path of the test DB is writable; the original workspace supplies
    # existing input/result files for GET. New configuration files are redirected below.
    db.workspace = workspace
    db.path = args.output / 'isolated.sqlite3'
    with sqlite3.connect('file:' + (workspace / 'database/hydrology.sqlite3').as_posix() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(db.path) as target:
            source.backup(target)
    original_path = calibration_configuration._path
    def configuration_path(database, identifier):
        old = original_path(database, identifier)
        return old if old.is_file() else args.output / 'calibration-configurations' / old.name
    server = __import__('http.server', fromlist=['ThreadingHTTPServer']).ThreadingHTTPServer(('127.0.0.1', 0), IsolatedHandler)
    server.db = db
    server.jobs = SimpleNamespace(lock=threading.RLock(), restore_pending=False, restart_required=False,
                                  backend_source_identity=model_identity(), public=Jobs.public)
    server.instance_id = 'isolated-scheme-management'
    base = 'http://127.0.0.1:' + str(server.server_port)
    errors, api_errors, posts, screens, cases = [], [], [], [], []
    report = {'createdAt': now(), 'scope': __doc__, 'productionHealth': health,
              'isolatedDatabase': str(db.path), 'passed': False}
    with ExitStack() as stack:
        stack.enter_context(patch.object(calibration_configuration, '_path', configuration_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for width, height in ((1366, 768), (1920, 1080)):
                        print('开始界面验收 ' + str(width), flush=True)
                        page = browser.new_page(viewport={'width': width, 'height': height})
                        page.on('pageerror', lambda err: errors.append(str(err)))
                        page.on('response', lambda response: api_errors.append({'url': response.url, 'status': response.status})
                                if '/api/' in response.url and response.status >= 400 else None)
                        page.on('request', lambda request: posts.append(request.url) if request.method == 'POST' else None)
                        page.goto(base + '/?page=calibration', wait_until='networkidle', timeout=90000)
                        page.locator('.calibration-workbench[data-coverage-ready="true"]').wait_for(timeout=90000)
                        page.get_by_role('button', name='新建方案', exact=True).click()
                        field = page.get_by_label('方案名称', exact=True)
                        field.wait_for()
                        name = '界面验收方案-' + str(width)
                        field.fill(name)
                        with page.expect_response(lambda response: response.url == base + '/api/model/schemes' and response.request.method == 'POST') as response:
                            page.get_by_role('button', name='保存方案', exact=True).click()
                        first = response.value.json()
                        assert 'id' in first, first
                        print('已新建 ' + first['id'], flush=True)
                        page.get_by_role('button', name='保存修订', exact=True).wait_for()
                        page.wait_for_function('()=>document.querySelector(".calibration-workbench .message.success")?.textContent.includes("已保存")')
                        page.get_by_role('button', name='复制为我的方案', exact=True).click()
                        copied_name = name + '-副本'
                        field.fill(copied_name)
                        page.get_by_role('button', name='参数与范围', exact=False).click()
                        page.get_by_label('张力水容量下界', exact=True).fill('41')
                        with page.expect_response(lambda response: response.url == base + '/api/model/schemes' and response.request.method == 'POST') as response:
                            page.get_by_role('button', name='保存方案', exact=True).click()
                        second = response.value.json()
                        print('已复制 ' + str(second.get('id')), flush=True)
                        assert first['payload']['familyId'] != second['payload']['familyId']
                        assert first['payload']['search']['bounds']['WM'][0] == 40
                        assert second['payload']['search']['bounds']['WM'][0] == 41
                        page.wait_for_function('()=>document.querySelector(".calibration-workbench .message.success")?.textContent.includes("已保存")')
                        ids = []
                        for index, row in enumerate((first, second)):
                            identifier = 'JOB-ui-scheme-' + str(width) + '-' + str(index)
                            ids.append(identifier)
                            with db.connect() as connection:
                                connection.execute('''INSERT INTO tasks(id,kind,status,created_at,updated_at,request,trashed,name)
                                    VALUES(?,?,?,?,?,?,?,?)''', (identifier, 'trial', 'cancelled', now(), now(),
                                    canonical({'researchSchemeId': row['id']}), 0, name + ' · 隔离取消记录' + str(index)))
                        old_trash = 'JOB-ui-scheme-pretrashed-' + str(width)
                        with db.connect() as connection:
                            connection.execute('''INSERT INTO tasks(id,kind,status,created_at,updated_at,request,trashed,name)
                                VALUES(?,?,?,?,?,?,?,?)''', (old_trash, 'trial', 'cancelled', now(), now(),
                                canonical({'researchSchemeId': first['id']}), 1, name + ' · 原已回收记录'))
                        page.get_by_role('button', name='我的方案', exact=True).click()
                        dialog = page.get_by_role('dialog', name='我的方案', exact=True)
                        dialog.locator('[data-scheme="' + first['id'] + '"]').wait_for(timeout=60000)
                        current = dialog.locator('tr').filter(has_text='当前正式方案').get_by_role('checkbox')
                        assert current.is_disabled()
                        for row in (first, second):
                            dialog.get_by_role('checkbox', name='选择方案 ' + row['name'], exact=True).check()
                        with page.expect_response(lambda response: response.url.endswith('/schemes/recycle-preview')) as response:
                            dialog.get_by_role('button', name='移入回收站（含关联结果）', exact=True).click()
                        plan = response.value.json()
                        assert (plan['schemeCount'], plan['resultCount'], plan['alreadyTrashedResultCount']) == (2, 2, 1), plan
                        expect(dialog.get_by_role('button', name='确认移入回收站', exact=True)).to_be_enabled()
                        preview_file = args.output / ('回收预览-' + str(width) + '.png')
                        page.screenshot(path=str(preview_file), full_page=True)
                        screens.append(preview_file)
                        dialog.get_by_role('button', name='确认移入回收站', exact=True).click()
                        restore_button = dialog.get_by_role('button', name='恢复方案与结果', exact=True).last
                        restore_button.wait_for(timeout=60000)
                        assert all(db.task(identifier)['trashed'] for identifier in ids)
                        assert all(db.setting('scheme-hidden:' + row['payload']['familyId']) for row in (first, second))
                        restore_button.click()
                        page.wait_for_function('()=>document.querySelector(".scheme-manager .message.success")?.textContent.includes("已恢复")')
                        assert all(not db.task(identifier)['trashed'] for identifier in ids)
                        assert db.task(old_trash)['trashed']
                        dialog.get_by_role('button', name='已保存方案', exact=True).click()
                        row = dialog.locator('[data-scheme="' + first['id'] + '"]')
                        row.get_by_role('button', name='打开设置', exact=True).click()
                        page.get_by_label('方案名称', exact=True).wait_for()
                        assert page.get_by_label('方案名称', exact=True).input_value() == name
                        page.get_by_role('button', name='参数与范围', exact=False).click()
                        assert float(page.get_by_label('张力水容量下界', exact=True).input_value()) == 40
                        expect(page.get_by_role('button', name='新建方案', exact=True)).to_be_enabled(timeout=60000)
                        saved_file = args.output / ('独立方案-' + str(width) + '.png')
                        page.screenshot(path=str(saved_file), full_page=True)
                        screens.append(saved_file)
                        overflow = page.evaluate('document.documentElement.scrollWidth>innerWidth')
                        assert not overflow
                        cases.append({'viewport': [width, height], 'firstScheme': first['id'], 'copiedScheme': second['id'],
                                      'independentFamilies': True, 'independentBounds': True, 'currentProtected': True,
                                      'recycledAndRestoredResults': ids, 'previouslyRecycledResultStayedRecycled': True,
                                      'restoredSchemeReopened': True, 'horizontalOverflow': False})
                        page.wait_for_load_state('networkidle', timeout=90000)
                        page.close()
                finally:
                    browser.close()
            assert not errors, errors
            assert not api_errors, api_errors
            assert all(any(path in url for path in ('/schemes',)) for url in posts), posts
            after = production_snapshot(workspace)
            assert before == after, '生产库或配置目录被验收改变'
            report.update(passed=True, cases=cases, pageErrors=errors, apiErrors=api_errors,
                          postCount=len(posts), productionUnchanged=True,
                          screenshots=[{'path': str(path.relative_to(ROOT)),
                                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in screens])
        finally:
            server.shutdown()
            server.server_close()
            with (args.output / '验收记录.json').open('x', encoding='utf-8') as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()

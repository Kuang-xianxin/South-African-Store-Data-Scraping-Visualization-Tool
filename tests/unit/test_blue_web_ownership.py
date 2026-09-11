from __future__ import annotations

import importlib
import json
from pathlib import Path
import threading
from urllib.parse import urlencode

import pytest


@pytest.fixture
def routing(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'scripts/ha'))
    return importlib.import_module('blue_web_router'), importlib.import_module('blue_web_routes')


def nodes(module, clock, path=None):
    from blue_web_routes import FAMILIES
    router = module.Router('a' * 64, 'b' * 64, state_path=path, wall=lambda: clock[0], clock=lambda: clock[0])
    for name, available in [('main', 60), ('laptop', 10)]:
        router.update(name, {'node': name, 'frontend_sha256': 'b' * 64, 'available_bytes': available,
            'total_bytes': 100, 'available_commit_bytes': 100, 'routing_version': 2, 'files': {},
            'workflows': {f: {'busy': False, 'present': False, 'updated': 0} for f in FAMILIES}}, 0)
    return router


@pytest.mark.parametrize('method,uri', [
    ('POST', '/api/auth/stores'), ('PATCH', '/api/auth/users/2'),
    ('PUT', '/api/competitors/personal-watchlist/12'),
    ('DELETE', '/api/competitors/personal-watchlist/libraries/3'),
    ('POST', '/api/competitors/targets'),
    ('PATCH', '/api/competitors/targets/123'), ('POST', '/api/erp/logistics/links'),
    ('POST', '/api/competitors/distributed/start-full'), ('POST', '/api/competitors/distributed/stop'),
    ('POST', '/api/competitors/distributed/resume'), ('GET', '/api/competitors/distributed/status'),
    ('GET', '/api/erp/search-ranking/123'), ('GET', '/api/new-unknown-route')])
def test_shared_database_actions_use_available_node(routing, method, uri):
    module, _ = routing
    router = nodes(module, [1000])
    assert router.choose(method, uri)[0] == 'main'
    del router.samples['main']
    assert router.choose(method, uri)[0] == 'laptop'


def test_parallel_starts_and_restart_keep_durable_owner(routing, tmp_path):
    module, _ = routing
    clock = [1000]
    path = tmp_path / 'ownership.json'
    router = nodes(module, clock, path)
    selected = []
    threads = [threading.Thread(target=lambda: selected.append(router.choose('POST', '/api/erp/search-ranking/batch/start')[0])) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert selected == ['main'] * 10
    router = nodes(module, clock, path)
    router.samples['main']['available_bytes'] = 1
    assert router.choose('POST', '/api/erp/search-ranking/batch/start')[0] == 'main'
    assert router.choose('POST', '/api/erp/search-ranking/batch/stop')[0] == 'main'
    clock[0] += 121
    for sample in router.samples.values():
        sample['received'] = clock[0]
    router.samples['main']['workflows']['search']['busy'] = True
    assert router.choose('POST', '/api/erp/search-ranking/batch/start')[0] == 'main'
    router.samples['main']['workflows']['search']['busy'] = False
    assert router.choose('POST', '/api/erp/search-ranking/batch/start')[0] == 'laptop'
    assert json.loads(path.read_text())['owners']['search']['node'] == 'laptop'


def test_existing_task_and_offline_owner_are_not_silently_moved(routing):
    module, _ = routing
    clock = [1000]
    router = nodes(module, clock)
    router.samples['laptop']['workflows']['search'] = {'present': True, 'busy': True, 'updated': 42}
    assert router.choose('GET', '/api/erp/search-ranking/batch/status')[0] == 'laptop'
    clock[0] += 13
    router.samples['main']['received'] = clock[0]
    assert router.choose('POST', '/api/erp/search-ranking/batch/stop')[0] is None
    assert router.choose('POST', '/api/erp/search-ranking/batch/start')[0] is None


def test_download_follows_actual_file_even_after_new_job_moves(routing, tmp_path):
    module, paths = routing
    clock = [1000]
    router = nodes(module, clock)
    day = '2026-09-09'
    file = tmp_path / 'exports' / day / ('Takealot运营日报_' + day + '.xlsx')
    file.parent.mkdir(parents=True)
    file.write_bytes(b'old report')
    router.samples['laptop']['files'] = paths.artifact_inventory(tmp_path)
    uri = '/api/erp/exports/download?' + urlencode({'kind': 'excel', 'as_of': day})
    assert router.choose('POST', '/api/erp/exports')[0] == 'main'
    assert router.choose('GET', uri)[0] == 'laptop'
    assert router.choose('GET', '/api/erp/exports?as_of=' + day)[0] == 'laptop'
    router.samples['main']['files'] = {paths.artifact_key(uri): file.stat().st_mtime_ns - 1}
    clock[0] += 13
    router.samples['main']['received'] = clock[0]
    assert router.choose('GET', uri)[0] is None
    assert paths.artifact_key('/api/erp/nft102/download?report_date=2026-09-09&name=..%2Fsecrets.txt') is None


def test_encoded_paths_do_not_bypass_ownership_and_local_crawl_blocks_new_distributed_start(routing):
    module, paths = routing
    assert paths.policy('POST', '/api%2Ferp/search-ranking/batch/stop')[0] == 'search'
    assert paths.policy('POST', '/api/erp/../erp/search-ranking/batch/stop')[0] == 'search'
    router = nodes(module, [1000])
    router.samples['laptop']['workflows']['legacy']['busy'] = True
    assert router.choose('POST', '/api/competitors/targets')[0] == 'laptop'
    assert router.choose('POST', '/api/competitors/distributed/start-full')[0] is None
    assert router.choose('POST', '/api/competitors/distributed/stop')[0] == 'main'


def test_real_two_app_session_write_and_csrf_survive_node_change(routing, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from takealot_ops.erp.web import create_app
    from blue_web_capacity import CapacityMiddleware
    from blue_web_inventory import Inventory

    monkeypatch.setenv('TAKEALOT_DATABASE_URL', 'sqlite:///' + (tmp_path / 'shared.db').as_posix())
    monkeypatch.setenv('TAKEALOT_WEB_ONLY', '1')
    monkeypatch.delenv('TAKEALOT_BLUE_READ_REPLICA', raising=False)
    monkeypatch.setattr(CapacityMiddleware, 'push', lambda self: self.inventory.snapshot())
    apps = []
    for node in ('main', 'laptop'):
        root = tmp_path / node
        (root / 'app').mkdir(parents=True)
        app = create_app(root / 'app')
        inventory = Inventory(app, root)
        assert not any(x['busy'] for x in inventory.snapshot()['workflows'].values())
        app.add_middleware(CapacityMiddleware, root=root, node=node, secret='a' * 64, inventory=inventory)
        apps.append(app)
    with TestClient(apps[0], base_url='https://blue.test', client=('127.0.0.1', 50000)) as main, TestClient(apps[1], base_url='https://blue.test', client=('127.0.0.1', 50001)) as laptop:
        response = main.post('/api/auth/bootstrap',
                             json={'username': 'testadmin', 'display_name': 'Test', 'password': 'test-password-123'})
        assert response.status_code == 200, response.text
        csrf = response.json()['csrf_token']
        laptop.cookies.update(main.cookies)
        assert laptop.get('/api/auth/session').json()['user']['username'] == 'testadmin'
        assert laptop.post('/api/auth/stores', json={'code': 'test', 'display_name': 'Test'}).status_code == 403
        response = laptop.post('/api/auth/stores', headers={'X-CSRF-Token': csrf}, json={'code': 'test', 'display_name': 'Test'})
        assert response.status_code == 200, response.text
        store = response.json()['store']
        response = main.patch('/api/auth/stores/' + str(store['id']), headers={'X-CSRF-Token': csrf}, json={'display_name': 'Updated'})
        assert response.status_code == 200, response.text
        assert any(row['display_name'] == 'Updated' for row in laptop.get('/api/auth/stores').json()['items'])
        assert main.post('/api/erp/search-ranking/batch/stop', headers={'X-CSRF-Token': csrf}).status_code == 409
        day = '2026-09-09'
        workbook = tmp_path / 'laptop/app/exports' / day / ('Takealot运营日报_' + day + '.xlsx')
        workbook.parent.mkdir(parents=True)
        workbook.write_bytes(b'isolated-test-workbook')
        router = nodes(routing[0], [1000])
        router.samples['laptop']['files'] = routing[1].artifact_inventory(tmp_path / 'laptop/app')
        uri = '/api/erp/exports/download?as_of=' + day + '&kind=excel'
        target = {'main': main, 'laptop': laptop}[router.choose('GET', uri)[0]]
        downloaded = target.get(uri)
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == b'isolated-test-workbook'

"""Read controller metadata without calling status endpoints or changing checkpoints."""
from __future__ import annotations

import inspect
from pathlib import Path
import threading
import time
from uuid import uuid4

from blue_web_routes import FAMILIES, artifact_inventory


class Inventory:
    def __init__(self, app, root: Path):
        self.app, self.root = app, root
        self.lock = threading.Lock()
        self.inflight = dict.fromkeys(FAMILIES, 0)
        self.mutation = uuid4().hex
        self.boot = uuid4().hex
        self.revision = None
        self.locals = {}
        for route in app.routes:
            endpoint = getattr(route, 'endpoint', None)
            if inspect.isfunction(endpoint):
                self.locals.update(inspect.getclosurevars(endpoint).nonlocals)

    def enter(self, family):
        with self.lock:
            if family:
                self.inflight[family] += 1

    def leave(self, family, mutated):
        with self.lock:
            if family:
                self.inflight[family] -= 1
            if mutated:
                self.mutation = uuid4().hex

    def invalidate(self, revision):
        if not revision or revision == self.revision:
            return
        self.revision = revision
        self.app.state.data_revisions.invalidate()
        self.app.state.read_projection_cache.clear()

    def snapshot(self):
        observed = time.monotonic_ns()
        with self.lock:
            workflows = {name: {'busy': bool(count), 'present': False, 'updated': 0}
                         for name, count in self.inflight.items()}
            mutation = self.mutation
        controller = self.app.state.search_ranking_batch_controller
        with controller._state_lock:
            state = controller._state
            workflows['search']['present'] = bool(state.get('batch_id'))
            workflows['search']['busy'] |= controller.analysis_lock.locked() or state.get('status') in {
                'queued', 'running', 'pausing', 'paused', 'stopping', 'interrupted'}
            if controller.state_path.is_file():
                workflows['search']['updated'] = controller.state_path.stat().st_mtime_ns
        legacy = self.locals['collection_registry']
        with legacy._lock:
            workflows['legacy']['busy'] |= bool(legacy._state.active)
            workflows['legacy']['present'] = bool(legacy._state.batch_id or legacy._journal)
            if legacy._journal_path and legacy._journal_path.is_file():
                workflows['legacy']['updated'] = legacy._journal_path.stat().st_mtime_ns
        previews = self.locals['listing_preview_registry']
        with previews._lock:
            workflows['listing']['busy'] |= any(item.expires_at > time.monotonic() for item in previews._items.values())
            workflows['listing']['present'] = workflows['listing']['busy']
        workflows['refresh']['busy'] |= self.locals['refresh_coordinator']._in_progress
        return {'routing_version': 2, 'mutation': mutation, 'boot': self.boot, 'inventory_at': observed, 'workflows': workflows,
                'files': artifact_inventory(self.root / 'app')}

"""Persist logical controller ownership; business jobs and data remain on their nodes."""
from __future__ import annotations

import json
from pathlib import Path
import time

from blue_web_routes import FAMILIES


class Ownership:
    def __init__(self, path: Path | None, *, wall=time.time, initial_owner=None):
        self.path, self.wall = path, wall
        self.owners = {f: {'node': initial_owner, 'until': 0} for f in FAMILIES} if initial_owner else {}
        self.revision = 1
        if path and path.exists():
            state = json.loads(path.read_text())
            if state.get('version') != 2:
                raise ValueError('Unknown ownership state')
            self.owners, self.revision = state['owners'], state['revision']
            if any(f not in FAMILIES or row['node'] not in {'main', 'laptop'} for f, row in self.owners.items()):
                raise ValueError('Invalid ownership state')

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.next')
            temporary.write_text(json.dumps({'version': 2, 'owners': self.owners, 'revision': self.revision}))
            temporary.replace(self.path)

    def changed(self):
        self.revision += 1
        self.save()

    def choose(self, family, new, live, samples, best):
        row = self.owners.get(family)
        busy = [name for name, s in samples.items() if s.get('workflows', {}).get(family, {}).get('busy')]
        # A stale/offline owner is never taken over implicitly. Conflicting live
        # controllers fail closed instead of hiding one user's running task.
        if len(busy) > 1:
            return None
        if busy:
            node = busy[0]
            if row and row['node'] != node and row.get('until', 0) > self.wall():
                return None
        elif row:
            node = row['node']
        else:
            historical = [(s.get('workflows', {}).get(family, {}).get('updated', 0), name)
                          for name, s in samples.items() if s.get('workflows', {}).get(family, {}).get('present')]
            node = max(historical)[1] if historical else best
        if node is None or node not in live:
            return None
        both_verified = set(live) == {'main', 'laptop'} and all(
            s.get('routing_version') == 2 and family in s.get('workflows', {}) for s in live.values())
        if new and not busy and both_verified and (not row or row.get('until', 0) <= self.wall()):
            node = best
        # Unassigned stateful families require both nodes to be inventoried first.
        if not row and not both_verified:
            return None
        if not row or row['node'] != node or (new and not busy and row.get('until', 0) <= self.wall()):
            self.owners[family] = {'node': node, 'until': self.wall() + 120}
            self.save()  # Durably reserve before forwarding the first request.
        return node

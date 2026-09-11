"""BLUE request dependencies shared by the ingress and node inventory."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import posixpath
from urllib.parse import parse_qs, unquote, urlsplit

FAMILIES = ('search', 'legacy', 'listing', 'refresh', 'exports', 'daily-export', 'nft', 'portal', 'returns', 'login')


def policy(method: str, uri: str) -> tuple[str, bool]:
    """Empty family means shared DB/stateless; bool permits a NEW idle workflow."""
    path = posixpath.normpath('/' + unquote(urlsplit(uri).path).lstrip('/')).rstrip('/')
    write = method not in {'GET', 'HEAD', 'OPTIONS'}
    if path in {'/api/auth/login', '/api/auth/bootstrap'}:
        return 'login', False  # Preserve the process-local login limiter.
    if path.startswith('/api/erp/platform-warehouse'):
        return 'portal', False  # OTP and one-use action tokens stay with their issuer.
    if path.startswith('/api/erp/returns/removal-orders'):
        return 'returns', False
    if path.startswith('/api/erp/search-ranking/batch') or (write and path.startswith('/api/erp/search-ranking/')):
        return 'search', path == '/api/erp/search-ranking/batch/start' or path.endswith('/analyze')
    if path in {'/api/erp/refresh', '/api/erp/refresh-status'}:
        return 'refresh', write and path.endswith('/refresh')
    if path.startswith('/api/erp/daily-report/export'):
        return 'daily-export', write and path.endswith('/export')
    if path.startswith('/api/erp/exports'):
        return 'exports', write and path == '/api/erp/exports'
    if path.startswith('/api/erp/nft102/') and not path.endswith('/inspect'):
        return 'nft', write and path.endswith('/generate')
    if path in {'/api/competitors/listing-preview', '/api/competitors/listing-targets'}:
        return 'listing', path.endswith('/listing-preview')
    if (path.startswith('/api/competitors/batch-') or path == '/api/competitors/collection-logs'
            or path == '/api/competitors/collect' or path.endswith('/prioritize')
            or (write and path == '/api/competitors/targets')):
        return 'legacy', False  # Existing browser journals must keep their original owner.
    return '', False


def artifact_key(uri: str) -> str | None:
    parsed = urlsplit(uri)
    query = parse_qs(parsed.query, keep_blank_values=True)
    params = {
        '/api/erp/exports': ('as_of',),
        '/api/erp/exports/download': ('as_of', 'kind'),
        '/api/erp/daily-report/export': ('through',),
        '/api/erp/daily-report/export/download': ('through',),
        '/api/erp/nft102/download': ('report_date', 'name'),
    }.get(parsed.path)
    if not params or any(len(query.get(name, [])) != 1 for name in params):
        return None
    values = [query[name][0] for name in params]
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', values[0]):
        return None
    if 'name' in params and (not values[-1] or any(c in values[-1] for c in '/\\\x00:')):
        return None
    return hashlib.sha256(repr((parsed.path, values)).encode()).hexdigest()


def artifact_inventory(root: Path) -> dict[str, int]:
    """Metadata only. Exact approved filenames; never return local paths or contents."""
    from urllib.parse import urlencode
    found = {}

    def add(path, endpoint, query):
        # Reparse points may not export files outside the isolated BLUE application.
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            return
        key = artifact_key(endpoint + '?' + urlencode(query))
        if key:
            found[key] = max(found.get(key, 0), path.stat().st_mtime_ns)

    for directory in (root / 'exports').glob('????-??-??'):
        day = directory.name
        for kind, suffix in (('html', '.html'), ('excel', '.xlsx'), ('png', '.png')):
            path = directory / ('Takealot运营日报_' + day + suffix)
            add(path, '/api/erp/exports/download', {'as_of': day, 'kind': kind})
            add(path, '/api/erp/exports', {'as_of': day})
    for directory in (root / 'exports/operations-daily').glob('????-??-??'):
        day = directory.name
        for endpoint in ('/api/erp/daily-report/export', '/api/erp/daily-report/export/download'):
            add(directory / ('运营日报_' + day + '.xlsx'), endpoint, {'through': day})
    for directory in (root / 'outputs/nft102-daily').glob('????-??-??'):
        for path in directory.iterdir():
            add(path, '/api/erp/nft102/download', {'report_date': directory.name, 'name': path.name})
    return found

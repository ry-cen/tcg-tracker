"""Shared test isolation.

Delivery state lives in PostgreSQL (the dirty-target outbox) while most tests give each test its
own records store. Rows left delivered by an earlier test would claim records a fresh store
never received, so every test starts and ends with an empty outbox.

Under pytest-xdist each worker has its own database, projection directory, runner lock directory,
catalog fixture directory and mail file. scripts/verify_resources.py writes them to
$VERIFY_PYTEST_WORKER_ROOT/<worker>/env.json, and this module applies them before any test module
is imported. Tier markers: ``slow`` tests are left out by ``npm run verify -- --fast``; ``serial``
tests run in a second, single-process pass (scripts/run_pytest.py).

VERIFY_PYTEST_SHARD=K/N (scripts/run_pytest.py --shard, from npm run verify -- --shard) keeps the K-th of N
parts of the collected test files and deselects the rest; see shard_files.
"""
from collections import Counter
import json
import os
from pathlib import Path
import re

import pytest


def _isolate_xdist_worker():
    worker = os.environ.get('PYTEST_XDIST_WORKER')
    if not worker:
        return
    root = os.environ.get('VERIFY_PYTEST_WORKER_ROOT')
    if not root:
        raise RuntimeError('Parallel pytest needs per-worker databases; run it through npm run verify')
    settings = Path(root) / worker / 'env.json'
    if not settings.is_file():
        raise RuntimeError(f'No prepared verification resources for pytest worker {worker}')
    os.environ.update(json.loads(settings.read_text()))


_isolate_xdist_worker()


def pytest_configure(config):
    shard_setting(os.environ.get('VERIFY_PYTEST_SHARD', ''))  # a malformed shard stops the run before collection
    config.addinivalue_line('markers', 'slow(reason): launches real processes, waits out real time, sleeps or builds '
                                       'capacity-sized data; left out by npm run verify -- --fast')
    config.addinivalue_line('markers', 'serial(reason): asserts wall-clock bounds or launches real runner processes; '
                                       'runs alone after the parallel pass')


def shard_files(weights, total):
    """{file: shard index from 0 to total - 1} for {file: weight}. Whole files go to shards, heaviest first, each to
    the lightest shard so far (ties to the lower index), so every file lands in exactly one shard and the shards carry
    about the same weight. The result depends only on the files and weights, never on their order or on the process:
    each pytest-xdist worker and both of run_pytest.py's passes compute it on their own and must agree."""
    loads = [0] * total
    shards = {}
    for path in sorted(weights, key=lambda path: (-weights[path], path)):
        shard = min(range(total), key=lambda index: (loads[index], index))
        shards[path] = shard
        loads[shard] += weights[path]
    return shards


def shard_setting(value):
    """(K, N) from a VERIFY_PYTEST_SHARD value 'K/N' with 1 <= K <= N, or None when it is empty."""
    if not value:
        return None
    match = re.fullmatch(r'([1-9][0-9]*)/([1-9][0-9]*)', value)
    if not match or int(match[1]) > int(match[2]):
        raise pytest.UsageError(f'VERIFY_PYTEST_SHARD must be K/N with 1 <= K <= N, not {value!r}')
    return int(match[1]), int(match[2])


# tryfirst: the shard is chosen from everything collected, before -m and -k deselect anything in _pytest.mark, so the
# parallel pass (-m "not serial") and the serial pass (-m serial) split the files the same way. --lf and --ff wrap this
# hook and filter afterwards, within the shard.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    setting = shard_setting(os.environ.get('VERIFY_PYTEST_SHARD', ''))
    if setting is None:
        return
    number, total = setting
    # A file weighs its tests that are not marked slow: the fast tier, which CI shards, runs only those. A file of
    # slow tests weighs nothing but still lands in one shard, so the full tier is covered too.
    weights = Counter()
    for item in items:
        weights[item.nodeid.split('::', 1)[0]] += item.get_closest_marker('slow') is None
    shards = shard_files(weights, total)
    kept = [item for item in items if shards[item.nodeid.split('::', 1)[0]] == number - 1]
    dropped = [item for item in items if shards[item.nodeid.split('::', 1)[0]] != number - 1]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


def _empty_outbox():
    if not os.environ.get('DATABASE_URL'):
        return
    import psycopg
    from psycopg.errors import UndefinedTable
    with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as conn:
        try:
            conn.execute('DELETE FROM catalog_public_outbox')
        except UndefinedTable:
            pass  # A test database that is not migrated yet.


@pytest.fixture(autouse=True)
def empty_public_outbox():
    _empty_outbox()
    yield
    _empty_outbox()

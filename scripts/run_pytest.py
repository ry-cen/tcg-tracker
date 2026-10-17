"""Run api/tests for the verification gate; scripts/ci.sh calls it as
``run_pytest.py [--fast] [--shard K/N] -- [selectors]``.

Pass 1 runs every test not marked ``serial``. When the gate prepared per-worker databases (VERIFY_PYTEST_DATABASE_URLS,
see scripts/verify_resources.py) it runs them with pytest-xdist; ``--dist loadfile`` keeps each file's tests together and
in order. Pass 2 runs the ``serial`` tests in one process with nothing else running: they assert wall-clock bounds or
launch real runner processes. ``--fast`` leaves out ``slow`` tests in both passes. Selectors (node ids, paths, -k, -x)
apply to both passes; -m, -n, -p and --dist belong to this runner. ``--shard K/N`` runs the K-th of N parts of the
test files in both passes: it sets VERIFY_PYTEST_SHARD for pytest, and api/tests/conftest.py splits the collected files
the same way in each pass and each xdist worker, so every test runs in exactly one shard.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ('pytest-parallel.xml', 'pytest-serial.xml')
RESERVED_LONG = ('--dist', '--numprocesses', '--max-worker-restart', '--junitxml', '--junit-xml', '--strict-markers')
RESERVED_SHORT = ('-m', '-n', '-p')


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fast', action='store_true')
    parser.add_argument('--shard', help='K/N: the K-th of N parts of the test files (api/tests/conftest.py checks it)')
    parser.add_argument('selectors', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    selectors = args.selectors[1:] if args.selectors[:1] == ['--'] else args.selectors
    return args.fast, args.shard, selectors


def marker_expressions(fast):
    """The -m expressions of (pass 1, pass 2)."""
    tail = ' and not slow' if fast else ''
    return 'not serial' + tail, 'serial' + tail


def reserved_argument(selectors):
    """The first selector argument that this runner sets itself, or None."""
    for argument in selectors:
        if argument.startswith('--'):
            if argument.split('=', 1)[0] in RESERVED_LONG:
                return argument
        elif argument[:2] in RESERVED_SHORT:
            return argument
    return None


def combined_status(codes):
    """0 when both passes passed. Exit status 5 (nothing collected) is fine for one pass, not for both."""
    if codes and all(code == 5 for code in codes):
        return 5
    return next((code for code in codes if code not in (0, 5)), 0)


def junit_counts(paths):
    counts = {'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0}
    for path in paths:
        if not Path(path).exists():
            continue
        for suite in ET.parse(path).getroot().iter('testsuite'):
            tests, failures, errors, skipped = (int(suite.get(key, 0)) for key in ('tests', 'failures', 'errors', 'skipped'))
            counts['passed'] += tests - failures - errors - skipped
            counts['failed'] += failures
            counts['errors'] += errors
            counts['skipped'] += skipped
    return counts


def commands(fast, workers, logs, selectors):
    first, second = marker_expressions(fast)
    common = ['.venv/bin/pytest', '-q', '--strict-markers', '--durations=40', '--durations-min=0']
    parallel = [*common, '-m', first, f'--junitxml={Path(logs) / REPORTS[0]}']
    parallel += ['-n', str(workers), '--dist', 'loadfile', '--max-worker-restart=0'] if workers > 1 else ['-p', 'no:xdist']
    serial = [*common, '-p', 'no:xdist', '-m', second, f'--junitxml={Path(logs) / REPORTS[1]}']
    targets = selectors or ['api/tests']
    return [('parallel', [*parallel, *targets]), ('serial', [*serial, *targets])]


def pytest_environment(base, shard):
    """The environment of both pytest passes: `base` with api on PYTHONPATH and VERIFY_PYTEST_SHARD set to `shard`. An
    unsharded run removes an inherited VERIFY_PYTEST_SHARD, which would otherwise split it without saying so."""
    environment = dict(base, PYTHONPATH='api')
    environment.pop('VERIFY_PYTEST_SHARD', None)
    if shard:
        environment['VERIFY_PYTEST_SHARD'] = shard
    return environment


def main(argv=None):
    fast, shard, selectors = parse(argv)
    reserved = reserved_argument(selectors)
    if reserved:
        print(f'run_pytest: {reserved} is set by the verification runner; select tests with node ids, paths or -k', file=sys.stderr)
        return 2
    workers = len(json.loads(os.environ.get('VERIFY_PYTEST_DATABASE_URLS') or '[]'))
    if workers > 1 and importlib.util.find_spec('xdist') is None:
        print('run_pytest: pytest-xdist is missing; install api/requirements.txt into .venv', file=sys.stderr)
        return 2
    logs = Path(os.environ.get('TEST_LOG_DIR') or '.')
    environment = pytest_environment(os.environ, shard)
    codes = []
    for name, command in commands(fast, workers, logs, selectors):
        print(f'== pytest {name} pass{f" (shard {shard})" if shard else ""}: {" ".join(command)}', flush=True)
        codes.append(subprocess.run(command, cwd=ROOT, env=environment).returncode)
    counts = junit_counts([logs / report for report in REPORTS])
    print(f"== pytest total: {counts['passed']} passed, {counts['failed']} failed, {counts['errors']} errors, "
          f"{counts['skipped']} skipped (both passes)", flush=True)
    return combined_status(codes)


if __name__ == '__main__':
    raise SystemExit(main())

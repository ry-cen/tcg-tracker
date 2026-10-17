"""Isolated verification. Optional selectors after --: pytest arguments with --suite api, Playwright with --suite browser.
--burn-in repeats the vitest files and Playwright specs changed against the merge base with --base (origin/dev).
--shard K/N runs the K-th of N parts of --suite api or --suite browser, as the CI legs of the fast tier do."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from verify_reaper import create_databases, current_owner, reap
from verify_report import network_changes, summarize

ROOT = Path(__file__).resolve().parents[1]
SUITES = ('all', 'api', 'browser', 'auth', 'operations')
# Burn-in budget. On a hosted 4-core runner (2 stacks) the fast tier's 144 Playwright tests took 395 s, about 2.75 s
# of wall time per test run, so 240 runs take about 11 minutes. Each changed spec's tests run 10 times when they fit the
# budget, fewer when they don't, and never fewer than 3. Each vitest repeat is a whole invocation, so it restarts
# every server the files start in beforeAll; 5 of them take about 2 minutes.
BURN_IN_BROWSER_RUNS = 240
BURN_IN_REPEATS = (3, 10)
BURN_IN_VITEST_REPEATS = 5


def default_workers():
    """(pytest workers, browser stacks) for this host: 6 and 4 on 12 cores, 2 and 2 on a 4-core CI runner."""
    cores = os.cpu_count() or 2
    return min(6, max(2, cores // 2)), min(4, max(2, cores // 3))


SHARD = re.compile(r'([1-9][0-9]*)/([1-9][0-9]*)')


def shard_argument(text):
    """argparse type of --shard: 'K/N' with 1 <= K <= N, returned as given."""
    match = SHARD.fullmatch(text)
    if not match or int(match[1]) > int(match[2]):
        raise argparse.ArgumentTypeError(f'{text!r} is not K/N with 1 <= K <= N')
    return text


def parse_arguments(argv=None):
    pytest_workers, browser_workers = default_workers()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--admin-url', default=os.environ.get('TEST_ADMIN_DATABASE_URL', os.environ.get('DATABASE_URL')))
    parser.add_argument('--suite', choices=SUITES, default='all')
    parser.add_argument('--fast', action='store_true', help='leave out pytest and vitest tests marked slow, Playwright tests tagged @slow, and the operations suite')
    parser.add_argument('--pytest-workers', type=int, default=pytest_workers, help='pytest-xdist workers, one database each')
    parser.add_argument('--browser-workers', type=int, default=browser_workers, help='Playwright workers, one stack each')
    parser.add_argument('--burn-in', action='store_true',
                        help='repeat the vitest files and Playwright specs changed against the merge base with --base')
    parser.add_argument('--base', default='origin/dev', help='the branch --burn-in compares with (default origin/dev)')
    parser.add_argument('--repeat', type=int,
                        help='runs of each test: Playwright --repeat-each and vitest invocations; with --burn-in, '
                             '--suite browser or --suite auth')
    parser.add_argument('--shard', type=shard_argument,
                        help='K/N: run the K-th of N parts of --suite api or --suite browser (whole test files); a browser '
                             'shard leaves guardrails and the build to another leg')
    parser.add_argument('selectors', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.selectors[:1] == ['--']:
        args.selectors = args.selectors[1:]
    if args.burn_in and (args.suite != 'all' or args.selectors):
        parser.error('--burn-in chooses its own tests: leave out --suite and selectors')
    if args.repeat is not None and not (args.burn_in or args.suite in ('browser', 'auth')):
        parser.error('--repeat needs --burn-in, --suite browser or --suite auth')
    if args.repeat is not None and args.repeat < 1:
        parser.error('--repeat must be at least 1')
    if args.shard and (args.burn_in or args.suite not in ('api', 'browser')):
        parser.error('--shard needs --suite api or --suite browser')
    if args.selectors and args.suite not in ('api', 'browser'):
        parser.error('Selectors after -- need --suite api or --suite browser')
    if args.pytest_workers < 1 or args.browser_workers < 1:
        parser.error('Worker counts must be at least 1')
    if not args.admin_url:
        parser.error('Set TEST_ADMIN_DATABASE_URL or supply --admin-url for a local disposable server')
    options = conninfo_to_dict(args.admin_url)
    if options.get('host') not in ('localhost', '127.0.0.1', '::1') or options.get('hostaddr', '127.0.0.1') not in ('127.0.0.1', '::1'):
        parser.error('Explicit loopback test database required')
    # Node pg needs a URI, not libpq's keyword connection format.
    if urlsplit(args.admin_url).scheme not in ('postgres', 'postgresql'):
        parser.error('Supply a PostgreSQL URI compatible with Python and Node')
    return args


def effective_workers(suite, pytest_workers, browser_workers):
    """Only the suites that run pytest or Playwright get extra databases and stacks; burn-in runs no pytest."""
    return (pytest_workers if suite in ('all', 'api') else 1), (browser_workers if suite in ('all', 'browser', 'burn-in') else 1)


def runs_operations(suite, fast):
    """The operations (release and recovery) suite runs in the full gate and in --suite operations, not under --fast."""
    return suite == 'operations' or (suite == 'all' and not fast)


def database_names(name, pytest_workers, browser_workers):
    """The run's own database, one per pytest-xdist worker when parallel, and one per extra browser stack."""
    workers = [f'{name}_gw{index}' for index in range(pytest_workers)] if pytest_workers > 1 else []
    return [name, *workers, *(f'{name}_s{number}' for number in range(1, browser_workers))]


def port_count(browser_workers):
    """Six ports for the gate's own stack and auth tests, three (api, auth, web) per extra browser stack."""
    return 6 + 3 * (browser_workers - 1)


def database_url(admin_url, name):
    return urlunsplit(urlsplit(admin_url)._replace(path='/' + name))


SUSPEND_LIMIT_S = 5.0     # wall time beyond the monotonic clock: the host slept (both clocks stop counting together otherwise)
SKEW_LIMIT_S = 2.0        # PostgreSQL's clock against this host's; approval-code expiry compares the two


def database_skew(admin_url):
    """Seconds by which the database server's clock is ahead of this host's (negative when it lags), or None."""
    try:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as conn:
            before = time.time()
            server = conn.execute('SELECT extract(epoch FROM clock_timestamp())').fetchone()[0]
            after = time.time()
        return round(float(server) - (before + after) / 2, 3)
    except psycopg.Error:
        return None


def clock_warnings(suspended, skews):
    """Lines that say this run's timing and expiry failures are not evidence: the host slept, or the database clock
    disagreed with the host's (Docker's VM clock lags after the host sleeps and catches up later)."""
    lines = []
    if suspended > SUSPEND_LIMIT_S:
        lines.append(f'the host was suspended for about {suspended:.0f} s during this run')
    for when, skew in skews.items():
        if skew is not None and abs(skew) > SKEW_LIMIT_S:
            lines.append(f'the database clock was {skew:+.1f} s off the host clock at the {when} of this run')
    return lines


def network_warnings(tests):
    """Lines naming failed Playwright tests during which Chromium dropped requests with net::ERR_NETWORK_CHANGED because
    the host's network configuration changed (an interface or address came or went). The dropped request may be
    unrelated to the failure, so the line asks for the trace to be checked rather than dismissing the failure."""
    return [f'Chromium dropped requests with net::ERR_NETWORK_CHANGED in {test}: the host network changed during that '
            'test. If its trace shows the dropped requests caused the failure (for example a blank page after module '
            'loads failed), the failure is not evidence: rerun it.' for test in tests]


def changed_files(base, root=ROOT):
    """(merge base, {path}) of the files added or changed since the merge base of HEAD and `base`, as the working tree
    has them: committed, staged, unstaged and untracked. Deleted files are left out."""
    merge_base = subprocess.check_output(['git', 'merge-base', 'HEAD', base], cwd=root, text=True).strip()
    changed = subprocess.check_output(['git', 'diff', '--name-only', '--diff-filter=d', '-z', merge_base], cwd=root, text=True)
    untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z'], cwd=root, text=True)
    return merge_base, {path for path in (changed + untracked).split('\0') if path}


def test_sources(root=ROOT):
    """{path: text} of every TypeScript file under auth/tests and web/tests."""
    return {path.relative_to(root).as_posix(): path.read_text()
            for directory in ('auth/tests', 'web/tests') for path in sorted((root / directory).rglob('*.ts'))}


IMPORT = re.compile(r'''\b(?:from|import)\s*["'](\.{1,2}/[^"']+)["']''')
FULLY_SLOW = re.compile(r'^// @module-tag slow\b', re.M)


def local_imports(source):
    """The relative module specifiers a TypeScript file imports or re-exports, such as "./support/motion"."""
    return IMPORT.findall(source)


def resolve_import(importer, specifier, known):
    """The file in `known` that `specifier`, imported by `importer`, names; None for anything else (src, packages)."""
    target = posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    stem = target[:-3] if target.endswith('.js') else target
    return next((path for path in (target, stem + '.ts', stem + '.tsx', stem + '/index.ts') if path in known), None)


def burn_in_selection(changed, sources, fast):
    """(vitest files, Playwright specs) to burn in: every test file that changed or imports a changed file under
    auth/tests or web/tests, directly or through other support files. A changed file no test imports (the Playwright
    config's browser-stack.ts) selects nothing. Under --fast a vitest file tagged `// @module-tag slow` drops out, since
    every test in it would be skipped."""
    graph = {path: {target for specifier in local_imports(text) if (target := resolve_import(path, specifier, sources))}
             for path, text in sources.items()}

    def reaches_change(start):
        stack, seen = [start], {start}
        while stack:
            path = stack.pop()
            if path in changed:
                return True
            for target in graph[path] - seen:
                seen.add(target)
                stack.append(target)
        return False

    vitest = sorted(path for path in sources if path.startswith('auth/tests/') and path.endswith('.test.ts')
                    and reaches_change(path) and not (fast and FULLY_SLOW.search(sources[path])))
    specs = sorted(path for path in sources if path.startswith('web/tests/') and path.endswith('.spec.ts') and reaches_change(path))
    return vitest, specs


def burn_in_repeats(tests):
    """Playwright --repeat-each for `tests` changed tests: as many repeats as BURN_IN_BROWSER_RUNS allows, within
    BURN_IN_REPEATS."""
    low, high = BURN_IN_REPEATS
    return max(low, min(high, BURN_IN_BROWSER_RUNS // max(tests, 1)))


def listed_tests(report):
    """The number of tests in a `playwright test --list --reporter=json` report."""
    count, suites = 0, list(report.get('suites', []))
    while suites:
        suite = suites.pop()
        suites.extend(suite.get('suites', []))
        count += sum(len(spec.get('tests', [])) for spec in suite.get('specs', []))
    return count


def playwright_test_count(specs, fast, root=ROOT):
    """How many tests the gate's two Playwright passes would run from `specs`, by Playwright's own listing."""
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'PLAYWRIGHT_BROWSERS_PATH') if key in os.environ}
    command = ['npx', 'playwright', 'test', '--list', '--reporter=json', '--pass-with-no-tests',
               *(['--grep-invert', '@slow'] if fast else []), *specs]
    listing = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True)
    if listing.returncode != 0:
        raise RuntimeError(f'playwright --list failed ({listing.returncode}): {(listing.stderr or listing.stdout)[-2000:]}')
    return listed_tests(json.loads(listing.stdout))


def plan_burn_in(base, fast, repeat=None, root=ROOT):
    """(plan, lines): the files --burn-in repeats and how often, and the `== burn-in:` lines that report them."""
    merge_base, changed = changed_files(base, root)
    vitest, specs = burn_in_selection(changed, test_sources(root), fast)
    tests = playwright_test_count(specs, fast, root) if specs else 0
    specs = specs if tests else []
    for path in (*vitest, *specs):
        if any(character.isspace() for character in path):
            raise ValueError(f'{path!r}: ci.sh takes burn-in paths separated by spaces')
    plan = {'base': base, 'merge_base': merge_base,
            'changed': sorted(path for path in changed if path.startswith(('auth/tests/', 'web/tests/'))),
            'vitest': vitest, 'specs': specs, 'playwright_tests': tests,
            'repeat': repeat or burn_in_repeats(tests), 'vitest_repeat': repeat or BURN_IN_VITEST_REPEATS}
    lines = [f"== burn-in: {len(plan['changed'])} changed file(s) under auth/tests and web/tests since {base} "
             f"(merge base {merge_base[:12]})"]
    if vitest:
        lines.append(f"== burn-in: vitest, {plan['vitest_repeat']} runs: {' '.join(vitest)}")
    if specs:
        lines.append(f"== burn-in: playwright --repeat-each {plan['repeat']}, {tests} tests: {' '.join(specs)}")
    if not vitest and not specs:
        lines.append('== burn-in: no changed vitest file or Playwright spec to repeat; nothing to run')
    return plan, lines


def repeat_settings(suite, repeat, plan):
    """(Playwright --repeat-each, vitest invocations) for ci.sh."""
    if plan:
        return plan['repeat'], plan['vitest_repeat']
    return (repeat or 1) if suite == 'browser' else 1, (repeat or 1) if suite == 'auth' else 1


def verify_settings(suite, fast, shard, workers, stacks, browser_repeat, vitest_repeat, plan):
    """The VERIFY_* settings scripts/ci.sh reads: the suite, tier and shard, the prepared pytest worker databases and
    extra browser stacks, the repeats, and the burn-in's files. An unsharded run sets VERIFY_SHARD empty."""
    return dict(VERIFY_SUITE=suite, VERIFY_FAST='1' if fast else '', VERIFY_SHARD=shard or '',
                VERIFY_OPERATIONS='1' if runs_operations(suite, fast) else '0',
                VERIFY_PYTEST_DATABASE_URLS=json.dumps(workers), VERIFY_EXTRA_STACKS=json.dumps(stacks),
                VERIFY_REPEAT=str(browser_repeat), VERIFY_VITEST_REPEAT=str(vitest_repeat),
                VERIFY_BURN_IN_VITEST=' '.join(plan['vitest']) if plan else '',
                VERIFY_BURN_IN_SPECS=' '.join(plan['specs']) if plan else '')


def gate_command(selectors):
    """ci.sh, kept from idle sleep where the host offers caffeinate (macOS). A closed lid on battery still sleeps."""
    command = ['bash', 'scripts/ci.sh', *selectors]
    return ['caffeinate', '-i', *command] if shutil.which('caffeinate') else command


def drop_databases(admin_url, name, created):
    """Drop the run's databases and anything else left under its name."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        leftovers = [row[0] for row in conn.execute('SELECT datname FROM pg_database WHERE starts_with(datname, %s)', (name + '_',))]
        for database in dict.fromkeys([*created, *leftovers]):
            conn.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(database)))


def main(argv=None):
    args = parse_arguments(argv)
    suite = 'burn-in' if args.burn_in else args.suite
    identifier = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4)
    artifacts = ROOT/'.scratch/verification'/identifier
    artifacts.mkdir(parents=True, mode=0o700)
    # Printed first, so a run that is stopped early still names its own directory (scripts/verify_discard.py).
    print(f'Verification run: {identifier}', flush=True)
    name = 'tcg_verify_'+secrets.token_hex(8)
    created = []
    result = None
    ports = []
    reservations = []
    plan = None
    try:
        if args.burn_in:
            # Chosen from the working tree before anything is created; with nothing to repeat the run ends here.
            try:
                plan, lines = plan_burn_in(args.base, args.fast, args.repeat)
            except (subprocess.CalledProcessError, RuntimeError, ValueError) as error:
                lines = [f'== burn-in: could not choose the tests: {error}']
            for line in lines:
                print(line, flush=True)
            (artifacts/'verification.log').write_text(''.join(line + '\n' for line in lines))
            if not plan or not (plan['vitest'] or plan['specs']):
                result = 0 if plan else 1
                (artifacts/'result.json').write_text(json.dumps({
                    'run': identifier, 'exit_code': result, 'suite': suite, 'selectors': [], 'browser_args': [],
                    'fast': args.fast, 'shard': None, 'burn_in': plan}))
                return result
        pytest_workers, browser_workers = effective_workers(suite, args.pytest_workers, args.browser_workers)
        if plan and not plan['specs']:
            browser_workers = 1  # vitest only: the gate's own stack is enough
        names = database_names(name, pytest_workers, browser_workers)
        browser_repeat, vitest_repeat = repeat_settings(suite, args.repeat, plan)
        # Before this run creates anything: drop the databases of verification runs that were killed (scripts/verify_reaper.py).
        try:
            reaper, reaped = reap(args.admin_url)
        except Exception as error:
            reaper, reaped = [f'== reaper: skipped: reaper error ({type(error).__name__})'], []
        for line in reaper:
            print(line, flush=True)
        with (artifacts/'verification.log').open('a') as log:
            log.write(''.join(line + '\n' for line in reaper))
        for _ in range(port_count(browser_workers)):
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            reservations.append(sock)
            ports.append(sock.getsockname()[1])
        with tempfile.TemporaryDirectory(prefix='tcg-verify-') as temp:
            checkout = Path(temp)/'checkout'
            checkout.mkdir()
            files = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
            for relative in files:
                if not relative or not (ROOT/relative).is_file():
                    continue
                target = checkout/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT/relative, target)
            for dependency in ('.venv', 'node_modules'):
                (checkout/dependency).symlink_to((ROOT/dependency).resolve(), target_is_directory=True)
            api, auth, web, signup, secondary, unavailable = ports[:6]
            extra = ports[6:]
            stacks = [{'database_url': database_url(args.admin_url, f'{name}_s{number}'), 'api_port': extra[3*number-3],
                       'auth_port': extra[3*number-2], 'web_port': extra[3*number-1]} for number in range(1, browser_workers)]
            workers = [database_url(args.admin_url, database) for database in names if database.startswith(name+'_gw')]
            env = dict({key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'PLAYWRIGHT_BROWSERS_PATH') if key in os.environ},
              DATABASE_URL=database_url(args.admin_url, name), RECOVERY_TEST_DATABASE_URL=args.admin_url,
              API_PORT=str(api), AUTH_PORT=str(auth), WEB_PORT=str(web),
              API_ORIGIN=f'http://127.0.0.1:{api}', TEST_ORIGIN=f'http://localhost:{web}',
              TEST_SIGNUP_PORT=str(signup), TEST_SECONDARY_AUTH_PORT=str(secondary), TEST_UNAVAILABLE_AUTH_PORT=str(unavailable),
              **verify_settings(suite, args.fast, args.shard, workers, stacks, browser_repeat, vitest_repeat, plan),
              TEST_LOG_DIR=str(artifacts), TEST_BROWSER_OUTPUT=str(artifacts/'browser'), RELEASE_TEST_HISTORY_ROOT=str(ROOT))
            with psycopg.connect(args.admin_url, autocommit=True) as conn:
                create_databases(conn, names, current_owner(identifier), created)
            for sock in reservations:
                sock.close()
            skews = {'start': database_skew(args.admin_url)}
            wall, steady = time.time(), time.monotonic()
            with (artifacts/'verification.log').open('a') as log:
                result = subprocess.run(gate_command(args.selectors), cwd=checkout, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
            suspended = round(max(0.0, (time.time() - wall) - (time.monotonic() - steady)), 1)
            skews['end'] = database_skew(args.admin_url)
            warnings = clock_warnings(suspended, skews)
            try:
                network = network_warnings(network_changes(artifacts))
            except Exception as error:  # an unreadable trace must not cost the run its summary and result.json
                network = [f'could not scan failed tests\' traces for network changes ({type(error).__name__})']
            if warnings or network:
                with (artifacts/'verification.log').open('a') as log:
                    log.write(''.join(f'== WARNING: {line}; wall-clock and expiry failures in this run are not evidence. '
                                      'Keep the host awake and rerun.\n' for line in warnings)
                              + ''.join(f'== WARNING: {line}\n' for line in network))
            try:
                timings = summarize(artifacts)
            except Exception as error:  # A broken report never changes the gate's result.
                timings = {'error': type(error).__name__}
            (artifacts/'result.json').write_text(json.dumps({
                'run': identifier, 'exit_code': result, 'suite': suite, 'selectors': args.selectors,
                'browser_args': args.selectors if suite == 'browser' else [], 'fast': args.fast, 'shard': args.shard,
                'pytest_workers': pytest_workers, 'browser_workers': browser_workers, 'ports': ports, 'databases': names,
                'database_removed': False, 'reaper': reaper, 'reaped_databases': reaped, 'timings': timings,
                'host_suspended_seconds': suspended,
                'database_clock_skew_seconds': skews, 'clock_warnings': warnings, 'network_warnings': network,
                'repeat': browser_repeat, 'vitest_repeat': vitest_repeat, 'burn_in': plan}))
    finally:
        for sock in reservations:
            sock.close()
        if created:
            drop_databases(args.admin_url, name, created)
            if (artifacts/'result.json').exists():
                report = json.loads((artifacts/'result.json').read_text())
                report['database_removed'] = True
                (artifacts/'result.json').write_text(json.dumps(report))
        if (artifacts/'result.json').exists():
            report = json.loads((artifacts/'result.json').read_text())
            for line in report.get('clock_warnings', []):
                print(f'WARNING: {line}; timing failures in this run are not evidence. Keep the host awake and rerun.')
            for line in report.get('network_warnings', []):
                print(f'WARNING: {line}')
        print(f'Verification artifacts: {artifacts}')
    return result if result is not None else 1


if __name__ == '__main__':
    raise SystemExit(main())

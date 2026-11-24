#!/usr/bin/env python3
"""agent-dispatcher: hand out tasks, git worktrees and reviews to many coding agents.

Every agent, whatever harness or model it runs in, only gets one instruction:
"Read AGENTS.md and follow it." The loop is a single command:

    python3 dispatcher.py next [ID]

Local only: it never pushes. Merges happen only through `accept`, after the
configured tests pass on the merged tree.
"""
import contextlib, fcntl, fnmatch, json, os, re, secrets, signal, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get('DISPATCHER_CONFIG', HERE / 'config.json'))


def load_config():
    if not CONFIG.exists():
        sys.exit(f'ERROR: no config at {CONFIG} (copy config.example.json to config.json)')
    c = json.loads(CONFIG.read_text())
    base = CONFIG.parent
    c['repo'] = (base / os.path.expanduser(c['repo'])).resolve()
    c['worktrees'] = (base / os.path.expanduser(c.get('worktrees', '..'))).resolve()
    c['state_dir'] = (base / os.path.expanduser(c.get('state_dir', '.'))).resolve()
    c['log'] = (base / os.path.expanduser(c.get('log', 'dispatcher-log.md'))).resolve()
    c['tasks'] = (base / os.path.expanduser(c.get('tasks', 'tasks.json'))).resolve()
    c.setdefault('main', 'main')
    c.setdefault('lease_seconds', 1800)
    c.setdefault('wait_seconds', 90)
    c.setdefault('max_agents', 12)
    c.setdefault('max_rejections', 4)
    c.setdefault('test_timeout', 1200)
    c.setdefault('tests', [])
    c.setdefault('env', {})
    c.setdefault('rules', [])
    c.setdefault('command', f'python3 -I {HERE / "dispatcher.py"}')
    return c


C = load_config()
REPO, MAIN = C['repo'], C['main']
LEASE, WAIT, MAX = C['lease_seconds'], C['wait_seconds'], C['max_agents']
CMD = C['command']


def load_tasks():
    """tasks.json: {"name": {"needs": [...], "files": [...], "brief": "..."}}"""
    t = json.loads(C['tasks'].read_text()) if C['tasks'].exists() else {}
    for k, v in t.items():
        if not isinstance(v.get('files'), list) or not isinstance(v.get('brief'), str):
            sys.exit(f'ERROR: task {k} needs "files" (list) and "brief" (string)')
        v.setdefault('needs', [])
        for d in v['needs']:
            if d not in t:
                sys.exit(f'ERROR: task {k} needs unknown task {d}')
    return t


TASKS = load_tasks()


class Error(Exception):
    pass


def git(*a, where=None):
    r = subprocess.run(['git', '-C', str(where or REPO), *map(str, a)], capture_output=True, text=True)
    if r.returncode:
        raise Error('git ' + a[0] + ': ' + (r.stderr.strip() or r.stdout.strip()))
    return r.stdout.strip()


def log(text):
    with open(C['log'], 'a') as f:
        f.write(f'- {time.strftime("%Y-%m-%d %H:%M:%S")} {text}\n')


def name_of(s, who):
    return s.get('names', {}).get(who, '?')


@contextlib.contextmanager
def state():
    C['state_dir'].mkdir(parents=True, exist_ok=True)
    with open(C['state_dir'] / '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        p = C['state_dir'] / 'state.json'
        s = json.loads(p.read_text()) if p.exists() else {'agents': {}, 'tasks': {}}
        update_proposals(s)
        for k in TASKS:
            s['tasks'].setdefault(k, dict(status='open', attempt=0, rejected=0))
        for k, t in s['tasks'].items():
            if t['status'] == 'work' and time.time() - s['agents'].get(t['who'], 0) > LEASE - 120:
                s['agents'][t['who']] = max(s['agents'].get(t['who'], 0), last_edit(t['dir']))
            if t['status'] in ('work', 'review') and time.time() - s['agents'].get(t['who'], 0) > LEASE:
                if t['status'] == 'work':
                    hand_over(k, t)
                t['status'] = 'open' if t['status'] == 'work' else 'ready'
        yield s
        tmp = C['state_dir'] / 'state.tmp'
        tmp.write_text(json.dumps(s, indent=1))
        os.replace(tmp, p)


def last_edit(where):
    """Newest change to an uncommitted file in a task folder: a working agent counts as alive."""
    try:
        r = subprocess.run(['git', '-C', where, 'status', '--porcelain', '-z', '-uall', '--no-renames'],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    newest = 0
    for e in r.stdout.split('\0'):
        if len(e) > 3:
            with contextlib.suppress(OSError):
                newest = max(newest, (Path(where) / e[3:]).lstat().st_mtime)
    return min(newest, time.time())


def built(p):
    """Build products and binaries do not belong in history."""
    if any(x.endswith('.dSYM') for x in p.parts) or p.suffix in ('.o', '.a', '.so', '.dylib', '.exe', '.dll'):
        return True
    if not p.is_file():
        return False
    with open(p, 'rb') as f:
        return b'\0' in f.read(8000)


def save(k, t):
    """Snapshot a task folder as a commit without touching its index or files."""
    w = Path(t['dir'])
    head = git('rev-parse', 'HEAD', where=w)
    if subprocess.run(['git', '-C', str(w), 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], capture_output=True).returncode == 0:
        return head, ['(a merge was in progress, only the last commit)']
    raw = subprocess.run(['git', '-C', str(w), 'status', '--porcelain', '-z', '-uall', '--no-renames'],
                         capture_output=True, text=True, check=True, timeout=20).stdout
    ok, rest = [], []
    for e in raw.split('\0'):
        if len(e) > 3:
            n, p = e[3:], w / e[3:]
            allowed = any(fnmatch.fnmatch(n, m) for m in TASKS[k]['files'])
            small = not p.is_symlink() and (not p.exists() or p.stat().st_size < 2_000_000)
            (ok if allowed and small and not built(p) else rest).append(n)
    if not ok:
        return head, rest
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(d, 'index'))

        def g(*a, text=None):
            return subprocess.run(['git', '-C', str(w), *a], env=env, input=text, capture_output=True,
                                  text=True, check=True, timeout=20).stdout.strip()
        g('read-tree', 'HEAD')
        g('add', '-A', '--', *ok)
        c = g('commit-tree', g('write-tree'), '-p', head,
              text=f'WIP: {k}, saved automatically when the lease expired\n\nUntested, finish before submitting.\n')
    git('branch', f"saved-{k}-{t['attempt']}-{time.time_ns()}", c)
    return c, rest


def hand_over(k, t):
    old = t.get('reason', '')
    if old.startswith('(handover)'):
        old = old.partition(' BEFORE: ')[2]
    try:
        c, rest = save(k, t)
        if c == git('rev-parse', MAIN):
            c = None
        else:
            t['commit'] = c
    except Exception as e:  # releasing a task must never fail on the snapshot
        c, rest = None, [f'(snapshot failed: {type(e).__name__})']
    note = f'Their state is in your branch ({c[:9]}, untested).' if c else 'There was no new state to save.'
    if rest:
        note += ' Not carried over: ' + ', '.join(rest) + '.'
    t['reason'] = (f"(handover) Not a rejection, {t['who']} stopped reporting. {note} "
                   f"Old folder {Path(t['dir']).name} is read-only. FIRST git merge {MAIN}, then finish."
                   + (f' BEFORE: {old}' if old else ''))
    log(f"{k} from {t['who']} handed over after the lease expired, snapshot {c[:9] if c else 'none'}")


def heartbeat(s, who):
    if who not in s['agents']:
        raise Error('unknown ID')
    s['agents'][who] = time.time()


def need(s, who, status):
    heartbeat(s, who)
    for k, t in s['tasks'].items():
        if t.get('who') == who and t['status'] == status:
            return k, t
    raise Error(f'{who} has no task in status {status}; run: next {who}')


def assign(s, who):
    for k, t in s['tasks'].items():
        if t.get('who') == who and t['status'] in ('work', 'review'):
            return k, t
    tasks = s['tasks']
    for k, t in tasks.items():
        if t['status'] == 'ready' and t.get('author') != who:
            n = t.get('reviews', 0) + 1
            w = C['worktrees'] / f'wt-review-{k}-{n}'
            git('worktree', 'add', '--detach', w, t['commit'])
            t.update(status='review', who=who, reviews=n, review_dir=str(w))
            log(f'{who} ({name_of(s, who)}) reviews {k}')
            return k, t
    for k, t in tasks.items():
        if t['status'] == 'open' and k in TASKS and all(tasks.get(d, {}).get('status') == 'done' for d in TASKS[k]['needs']):
            n = t['attempt'] + 1
            w, b = C['worktrees'] / f'wt-task-{k}-{n}', f'task-{k}-{n}'
            git('worktree', 'add', w, '-b', b, t.get('commit') or MAIN)
            t.update(status='work', who=who, attempt=n, dir=str(w))
            log(f'{who} ({name_of(s, who)}) works on {k} in {w.name}')
            return k, t


def show(who, k, t):
    print(f'ID {who}')
    rules = ' '.join(C['rules'])
    if t['status'] == 'work':
        print(f"TASK {k}\nFOLDER {t['dir']}\nBRIEF {TASKS[k]['brief']}\nALLOWED FILES {', '.join(TASKS[k]['files'])}")
        if t.get('reason'):
            print('LAST REJECTED: ' + t['reason'])
            print(f'On a conflict: git merge {MAIN} in FOLDER and resolve it there.')
        if rules:
            print('RULES ' + rules)
        print(f'THEN commit, then: {CMD} submit {who}')
    else:
        print(f"REVIEW {k}\nFOLDER {t['review_dir']}\nDIFF git diff {MAIN}...{t['commit']}\nBRIEF WAS {TASKS[k]['brief']}")
        if rules:
            print('RULES ' + rules)
        print('Read the whole diff. Reject stubs, fake successes, special cases and anything left undone.')
        print(f"THEN {CMD} accept {who}  OR  {CMD} reject {who} 'reason'")


def next_(who=None):
    end = time.time() + WAIT
    told = False
    while True:
        if (C['state_dir'] / 'STOP').exists():
            print('END: stopped by the user')
            return
        with state() as s:
            if who:
                heartbeat(s, who)
            else:
                if sum(time.time() - x < LEASE for x in s['agents'].values()) >= MAX:
                    print('END: enough agents active, change nothing')
                    return
                who = secrets.token_hex(3)
                s['agents'][who] = time.time()
            if who not in s.get('names', {}) and not told:
                print(f"NAME: report your model name once (what you are called or who trained you, e.g. 'Claude Opus 5.5' or 'GPT-5'): "
                      f"{CMD} name {who} 'model name'. Your ID stays {who}.", flush=True)
            m = assign(s, who)
            if m:
                show(who, *m)
                return
            q = assign_help(s, who)
            if q:
                print(f"ID {who}\nHELP {q['nr']} from {q['from']} ({name_of(s, q['from'])}):\n{q['text']}\n"
                      "Research or look it up (read only, change nothing in other folders, no merge), then answer briefly and concretely: "
                      f"{CMD} answer {who} {q['nr']} 'answer'. Then: next {who}")
                return
            v = assign_vote(s, who)
            if v:
                print(f"ID {who}\nVOTE {v['nr']}: proposal from {v['from']} ({name_of(s, v['from'])})\n"
                      f"NAME {v['name']}\nNEEDS {', '.join(v['needs']) or '-'}\nALLOWED FILES {', '.join(v['files'])}\nBRIEF {v['brief']}\n"
                      f"{VOTE_RULE}\nThen: {CMD} vote {who} {v['nr']} yes|no 'short reason'. Then: next {who}")
                return
            if all(t['status'] in ('done', 'halt') for t in s['tasks'].values()) and not any(
                    p['status'] == 'voting' for p in s.get('proposals', [])):
                print(f'ID {who}\nEND: nothing left to hand out. {PROPOSE_HINT}')
                return
        if not told:
            told = True
            print(f'Nothing free for {who} right now. Looking for up to {WAIT} s, this is not a hang.', flush=True)
        if time.time() >= end:
            print(f'ID {who}\n{PROPOSE_HINT}\nWAIT: run again right away: next {who}')
            return
        time.sleep(5)


def check_files(k, commit):
    names = git('diff', '--name-only', '--no-renames', f'{MAIN}...{commit}').splitlines()
    if not names:
        raise Error('nothing committed')
    foreign = [n for n in names if not any(fnmatch.fnmatch(n, m) for m in TASKS[k]['files'])]
    if foreign:
        raise Error('files not allowed: ' + ', '.join(foreign))


def submit(who):
    with state() as s:
        k, t = need(s, who, 'work')
        if git('status', '--porcelain', where=t['dir']):
            raise Error('commit everything first, do not leave test output lying around')
        c = git('rev-parse', 'HEAD', where=t['dir'])
        check_files(k, c)
        t.update(status='ready', commit=c, author=who)
        log(f'READY {c[:9]} {k} from {who} ({name_of(s, who)})')
    print(f'SUBMITTED. Now: next {who}')


def send_back(t, reason):
    t['rejected'] += 1
    t.update(status='halt' if t['rejected'] >= C['max_rejections'] else 'open', reason=reason)


def reject(who, *reason):
    reason = ' '.join(reason).strip()
    if not reason:
        raise Error("give a reason: reject ID 'reason'")
    with state() as s:
        k, t = need(s, who, 'review')
        send_back(t, reason)
        log(f"REJECTED {t['commit'][:9]} {k}: {reason} (now {t['status']})")
    print(f'REJECTED. Now: next {who}')


def conflict(who, files):
    """A merge conflict is not bad code and does not count towards halting."""
    names = ', '.join(files)
    with state() as s:
        k, t = need(s, who, 'review')
        t.update(status='open', reason=f'MERGE CONFLICT (not a rejection) in {names}: FIRST git merge {MAIN} in FOLDER, '
                                       'keep both sides, run the tests, commit, submit.')
        log(f"CONFLICT {t['commit'][:9]} {k}: {names} (not counted as a rejection)")
    print(f'CONFLICT, sent back without a rejection. Now: next {who}')


def run_tests(w):
    env = dict(os.environ)
    env.update({k: os.path.expanduser(str(v)) for k, v in C['env'].items()})
    out = 'no tests configured'
    for t in C['tests']:
        cwd = Path(w) / t.get('cwd', '.')
        if t.get('optional') and not cwd.exists():
            continue
        p = subprocess.Popen(t['run'], shell=True, cwd=cwd, env=env, text=True, errors='replace',
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            out = p.communicate(timeout=C['test_timeout'])[0]
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            return False, f"time limit {C['test_timeout']} s: {t['run']}"
        if p.returncode:
            return False, out[-1500:]
    return True, out[-1500:]


def accept(who, rnd='1'):
    # One merge test at a time: parallel runs slow each other down, and the
    # second one would have to retest on the new main anyway.
    with open(C['state_dir'] / '.merge', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _accept(who, rnd)


def _accept(who, rnd='1'):
    with state() as s:
        k, t = need(s, who, 'review')
        c, w = t['commit'], t['review_dir']
        check_files(k, c)
        base = git('rev-parse', MAIN)
    try:
        git('checkout', '--detach', base, where=w)
        try:
            git('merge', '--no-ff', '-m', f'Merge {k} ({c[:9]})', c, where=w)
        except Error:
            files = git('diff', '--name-only', '--diff-filter=U', where=w).split()
            if not files:
                raise
            git('merge', '--abort', where=w)
            return conflict(who, files)
        new = git('rev-parse', 'HEAD', where=w)
        ok, out = run_tests(w)
    except Error as e:
        ok, out = False, str(e)
    again = False
    with state() as s:
        k, t = need(s, who, 'review')
        if not ok:
            send_back(t, 'merge/tests red: ' + out)
            log(f'REJECTED {c[:9]} {k}: merge/tests red')
            print(f'RED, sent back.\n{out}\nNow: next {who}')
            return
        if git('symbolic-ref', '--short', 'HEAD') != MAIN or git('status', '--porcelain', '-uno'):
            raise Error('the main checkout is not clean on the main branch, nothing merged')
        if git('rev-parse', MAIN) != base:
            if int(rnd) >= 3:
                raise Error('main moved: run accept again')
            again = True
        else:
            git('merge', '--ff-only', new)
            t['status'] = 'done'
            log(f'MERGED {c[:9]} {k}, reviewed by {who} ({name_of(s, who)}), tests green')
    if again:
        print('Another merge was faster, testing once more on the new main.', flush=True)
        return _accept(who, str(int(rnd) + 1))
    print(f'MERGED. Now: next {who}')


def ping(who):
    with state() as s:
        heartbeat(s, who)
    print('ok')
    read_answers(who, only_new=True)


def assign_help(s, who):
    for q in s.get('questions', []):
        open_ = not q['answers'] and q['from'] != who
        free = not q.get('by') or time.time() - q.get('since', 0) > LEASE
        if open_ and free:
            q.update(by=who, since=time.time())
            log(f"{who} helps with question {q['nr']}")
            return q


def ask(who, *text):
    text = ' '.join(text).strip()[:4000]
    if not text:
        raise Error("usage: ask ID 'question or research request'")
    with state() as s:
        heartbeat(s, who)
        qs = s.setdefault('questions', [])
        nr = max((q['nr'] for q in qs), default=0) + 1
        qs.append(dict(nr=nr, **{'from': who}, text=text, time=time.time(), answers=[]))
    log(f'{who} asks ({nr}): {text[:120]}')
    print(f'Question {nr} posted. An idle agent will answer. See answers with: answers {who} (ping {who} shows them too). Keep working.')


def answer(who, nr, *text):
    text = ' '.join(text).strip()[:8000]
    if not text:
        raise Error("usage: answer ID NR 'answer'")
    with state() as s:
        heartbeat(s, who)
        q = next((q for q in s.get('questions', []) if str(q['nr']) == str(nr)), None)
        if not q:
            raise Error(f'no question {nr}')
        q['answers'].append(dict(by=who, text=text, time=time.time()))
        q['by'] = None
    log(f'{who} answers question {nr}')
    print(f'ok. Continue with: next {who}')


def read_answers(who, only_new=False):
    with state() as s:
        for q in s.get('questions', []):
            if q['from'] != who:
                continue
            new = [a for a in q['answers'] if not a.get('read')]
            shown = new if only_new else q['answers']
            if not only_new and not shown:
                print(f"Question {q['nr']}: no answer yet")
            for a in shown:
                print(f"ANSWER to question {q['nr']} from {a['by']} ({name_of(s, a['by'])}):\n{a['text']}")
                a['read'] = True


def set_name(who, *model):
    model = ' '.join(model).strip()[:60]
    if not model:
        raise Error("usage: name ID 'model name'")
    with state() as s:
        if who not in s['agents']:
            raise Error(f'unknown ID {who}')
        s.setdefault('names', {})[who] = model
        heartbeat(s, who)
    log(f'{who} is {model}')
    print(f'ok, {who} = {model}. Continue with: next {who}')


PROPOSE_HINT = ("PROPOSE: if you see a useful task that is missing (from a review, a report, a test gap), propose it: "
                f"{CMD} propose ID short-name 'path/pattern*,tests/x/*' 'brief: what is wrong, what done looks like, how it is tested' "
                "[needed-task,...]. The majority of active agents decides.")
VOTE_RULE = ("Vote YES only if the task is not already covered, is clearly testable, has narrow and fitting allowed files, "
             "fits one session, and follows the project rules. Otherwise NO with a reason.")
LOCKED_PATTERNS = ('*', '**', '.git*')
PROPOSAL_TTL = 3 * 3600


def update_proposals(s):
    """Decide open proposals by majority and turn accepted ones into tasks."""
    active = [w for w, x in s['agents'].items() if time.time() - x < LEASE]
    for p in s.get('proposals', []):
        if p['status'] == 'voting':
            voters = max(len([w for w in active if w != p['from']]), 2)
            yes, no = len(p['yes']), len(p['no'])
            if p['name'] in TASKS:
                p.update(status='rejected', reason='name taken meanwhile')
            elif yes >= 2 and yes * 2 > voters and yes > no:
                p['status'] = 'accepted'
                log(f"PROPOSAL {p['nr']} {p['name']} ACCEPTED ({yes} yes, {no} no)")
            elif no * 2 >= voters or time.time() - p['time'] > PROPOSAL_TTL:
                p['status'] = 'rejected'
                log(f"PROPOSAL {p['nr']} {p['name']} rejected ({yes} yes, {no} no)")
        if p['status'] == 'accepted' and p['name'] not in TASKS:
            TASKS[p['name']] = dict(needs=list(p['needs']), files=list(p['files']),
                                    brief=p['brief'] + f" (Proposal {p['nr']} by {p['from']}, accepted by majority.)")


def assign_vote(s, who):
    for p in s.get('proposals', []):
        if p['status'] == 'voting' and p['from'] != who and who not in p['yes'] + p['no']:
            return p


def propose(who, name='', files='', brief='', needs=''):
    name, brief = name.strip().lower(), brief.strip()[:4000]
    files = [f.strip() for f in files.split(',') if f.strip()]
    needs = [n.strip() for n in needs.split(',') if n.strip()]
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,40}', name) or not files or len(brief) < 40:
        raise Error("usage: propose ID short-name 'pattern1,pattern2' 'brief (40+ characters)' [needed1,needed2]")
    bad = [f for f in files if f in LOCKED_PATTERNS or f.startswith('/') or '..' in f]
    if bad:
        raise Error('file patterns not allowed: ' + ', '.join(bad))
    with state() as s:
        if who not in s['agents']:
            raise Error(f'unknown ID {who}')
        heartbeat(s, who)
        ps = s.setdefault('proposals', [])
        if name in TASKS or any(p['name'] == name and p['status'] == 'voting' for p in ps):
            raise Error(f'{name} already exists')
        missing = [n for n in needs if n not in TASKS]
        if missing:
            raise Error('unknown needed task: ' + ', '.join(missing))
        if sum(p['status'] == 'voting' and p['from'] == who for p in ps) >= 2:
            raise Error('you already have 2 open proposals, wait for the votes')
        nr = max((p['nr'] for p in ps), default=0) + 1
        ps.append(dict(nr=nr, **{'from': who}, name=name, needs=needs, files=files, brief=brief,
                       time=time.time(), yes=[], no=[], reasons=[], status='voting'))
    log(f'{who} proposes ({nr}): {name} [{", ".join(files)}] {brief[:120]}')
    print(f'Proposal {nr} posted. Idle agents will vote. Continue with: next {who}')


def vote(who, nr, choice='', *reason):
    choice, reason = choice.strip().lower(), ' '.join(reason).strip()[:1000]
    if choice not in ('yes', 'no') or not reason:
        raise Error("usage: vote ID NR yes|no 'short reason'")
    with state() as s:
        heartbeat(s, who)
        p = next((p for p in s.get('proposals', []) if str(p['nr']) == str(nr)), None)
        if not p or p['status'] != 'voting':
            raise Error(f'no open proposal {nr}')
        if p['from'] == who or who in p['yes'] + p['no']:
            raise Error('own proposal or already voted')
        p[choice].append(who)
        p['reasons'].append(dict(by=who, vote=choice, text=reason))
        update_proposals(s)
        result = p['status']
    log(f'{who} votes {choice} on proposal {nr}: {reason[:120]}')
    print(f'ok ({result}). Continue with: next {who}')


def drop(nr, *reason):
    """For humans: drop a proposal, also an accepted one nobody has started yet."""
    with state() as s:
        p = next((p for p in s.get('proposals', []) if str(p['nr']) == str(nr)), None)
        if not p:
            raise Error(f'no proposal {nr}')
        t = s['tasks'].get(p['name'])
        if t and t['status'] != 'open':
            raise Error(f"{p['name']} is already {t['status']}")
        p['status'] = 'dropped'
        s['tasks'].pop(p['name'], None)
        TASKS.pop(p['name'], None)
    log(f"PROPOSAL {nr} dropped: {' '.join(reason)}")
    print('ok')


def status():
    with state() as s:
        names = s.get('names', {})
        for k, t in s['tasks'].items():
            w = t.get('who', '-')
            print(f"{k:24} {t['status']:7} {w} {names.get(w, '') if w != '-' else ''}")
        for p in s.get('proposals', []):
            if p['status'] in ('voting', 'accepted'):
                print(f"proposal {p['nr']:<3} {p['name']:24} {p['status']:8} {len(p['yes'])} yes / {len(p['no'])} no, by {p['from']} {names.get(p['from'], '')}")
        print('active agents:')
        for w, x in s['agents'].items():
            if time.time() - x < LEASE:
                print(f"  {w}  {names.get(w, '(no name reported)')}")


COMMANDS = dict(next=next_, submit=submit, reject=reject, accept=accept, ping=ping, status=status,
                name=set_name, ask=ask, answer=answer, answers=read_answers,
                propose=propose, vote=vote, drop=drop)

if __name__ == '__main__':
    try:
        COMMANDS[sys.argv[1] if len(sys.argv) > 1 else 'next'](*sys.argv[2:])
    except (Error, KeyError, TypeError) as e:
        sys.exit(f'ERROR: {e}')

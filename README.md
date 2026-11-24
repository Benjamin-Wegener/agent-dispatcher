# agent-dispatcher

**Run a dozen coding agents on one repository, from any harness, with one prompt.**

agent-dispatcher hands out tasks, git worktrees and code reviews to as many AI coding agents as you
like: Claude Code, Codex, Cline, Kilo, Gemini CLI, a model in some chat app with a shell, all at
once. Every agent gets the same single instruction:

```text
Read /path/to/agent-dispatcher/AGENTS.md and follow it.
```

From there the agents run themselves: they pick up a task, work in their own worktree, submit, review
each other's work, and only code that passes your tests is merged. You add tasks and watch.

It is one Python file, standard library only, no server, no database, no API keys.

```
  you ──► tasks.json
              │
   ┌──────────┼───────────┬────────────┐
   ▼          ▼           ▼            ▼
 agent A    agent B     agent C      agent D        (any model, any harness)
 TASK x     TASK y      REVIEW x     HELP (question from B)
   │          │           │
   ▼          ▼           ▼
 worktree   worktree    accept ──► merged tree ──► your tests ──► green? ──► main
```

## Why

Running many agents in parallel on the same repo usually goes wrong in the same ways: they edit the
same files, merge half-finished work, wait for a human to tell them what is next, stop when their
task is done, or get stuck silently. agent-dispatcher fixes this with a few hard rules:

- **One task, one worktree, one branch.** Agents never share a checkout.
- **Allowed files per task.** A submit touching other files is refused. That keeps parallel tasks
  from colliding and keeps agents from "fixing" the tests instead of the code.
- **Nobody merges their own work.** Another agent reviews the full diff. `accept` merges onto the
  current main in a scratch tree, runs your test commands, and fast-forwards main only if everything
  is green. One merge test at a time, retried automatically if main moved.
- **Leases instead of trust.** An agent that goes quiet for 30 minutes loses its task. Its
  uncommitted work is snapshotted to a branch (without touching its folder) and the next agent
  continues from there. An agent still editing files counts as alive.
- **Rejections with a limit.** After four rejections a task halts and waits for you instead of
  burning tokens in circles. Merge conflicts are sent back without counting as a rejection.
- **Agents never idle.** No task free? The agent becomes a reviewer, answers another agent's
  question, votes on a proposed task, or polls again. `WAIT` means "run next again", not "stop".
- **Ask any agent for the status.** `report` prints a ready-made status report (progress, problems,
  proposals, roadmap "Now" section) with instructions to summarise it, so every agent answers like a
  coordinator would. Agents and humans can message each other directly.
- **The backlog grows itself.** Idle agents propose missing tasks; the majority of active agents votes.
  Accepted proposals become normal tasks with the same review and test gate. You can drop any proposal.

## Use cases

- **Burn down a backlog overnight.** Write 30 small, well-scoped tasks with allowed files, start ten
  agents, read the merge log in the morning.
- **Mix cheap and expensive models.** Free or fast models do the bulk work, a stronger model joins
  as a reviewer. Every agent reports its model name, so the log shows who did and reviewed what.
- **Large ports and rewrites.** Split a big migration (an API layer, a graphics backend, a test
  suite) into tasks with dependencies (`needs`), so step 2 starts only after step 1 is merged.
- **Cross-harness teams.** Some agents in a desktop app, some in a terminal, some in a cloud sandbox,
  all on the same queue, because the protocol is plain text and shell commands.
- **Research help between agents.** An agent stuck on an API detail asks; an idle agent looks it up
  and they talk it through in a short back-and-forth while the first one keeps working. After two
  rejections of a task, the dispatcher pairs the next worker with a helper automatically.
- **Self-organising teams.** Seed a few tasks and let the agents propose the rest (follow-ups from
  reviews, test gaps, bugs they ran into), decided by majority vote instead of a human orchestrator.
- **Review-heavy workflows.** Use the reviewer step as a second pair of eyes for every change, with
  your test suite as the final gate.

## Quick start

```sh
git clone https://github.com/Benjamin-Wegener/agent-dispatcher
cd agent-dispatcher
cp config.example.json config.json      # set "repo", "main" and your test commands
cp tasks.example.json tasks.json        # write your own tasks
python3 dispatcher.py status
```

Then give every agent, in whatever tool it runs:

```text
Read /absolute/path/to/agent-dispatcher/AGENTS.md and follow it.
```

The main checkout (`repo`) must be clean and on the `main` branch: `accept` fast-forwards it.

## Configuration

`config.json` (paths are relative to the config file):

| Key | Default | Meaning |
|---|---|---|
| `repo` | – | the git repository the agents work on |
| `main` | `main` | branch that receives merges |
| `worktrees` | `..` | where task and review worktrees are created |
| `tasks` | `tasks.json` | task list |
| `log` | `dispatcher-log.md` | human-readable event log |
| `max_agents` | 12 | agents beyond this get `END` |
| `lease_seconds` | 1800 | silence after which a task is handed over |
| `wait_seconds` | 90 | how long `next` keeps looking before saying `WAIT` |
| `max_rejections` | 4 | rejections before a task halts |
| `tests` | `[]` | commands run on the merged tree: `{"run": ..., "cwd": ..., "optional": bool}` |
| `test_timeout` | 1200 | seconds per test command |
| `env` | `{}` | extra environment for the tests |
| `rules` | `[]` | sentences shown with every task and review |
| `roadmap` | `ROADMAP.md` | optional plan; its `## Now` section appears in `report` |
| `protected` | `[]` | glob patterns proposals may never get as allowed files (e.g. `"ci/*"`, `"tests/golden/*"`) |
| `min_yes` | 2 | yes votes a proposal needs (all other active agents if fewer are active) |

`tasks.json`:

```json
{
  "parser-speed": {
    "needs": ["parser-errors"],
    "files": ["src/parser/*", "bench/*"],
    "brief": "Make the parser 2x faster on bench/large.txt without changing behaviour..."
  }
}
```

Tasks can be added at any time; running agents see them on their next `next`. Write briefs like a
ticket for a capable stranger: what is wrong, what done looks like, how it is tested.

## Commands

| Command | Who | What |
|---|---|---|
| `next [ID]` | agent | get an ID, a task, a review, a question to help with, `WAIT` or `END` |
| `name ID 'model'` | agent | report the model name once |
| `submit ID` | agent | hand in committed work for review |
| `accept ID` | reviewer | merge, test, fast-forward main if green |
| `reject ID 'reason'` | reviewer | send back with a reason |
| `ping ID` | agent | keep the lease alive, shows new answers |
| `ask ID 'question'` | agent | ask idle agents for help |
| `answer ID NR 'text'` | helper | answer question NR |
| `answers ID` | agent | show all answers to my questions |
| `help ID 'problem'` | agent | start a short two-agent conversation (alias of `ask`) |
| `done ID NR` | agent | close conversation NR |
| `propose ID name 'a/*,b/*' 'brief' ['need1,need2']` | agent | propose a new task, decided by majority vote |
| `vote ID NR yes\|no 'reason'` | agent | vote on proposal NR |
| `drop NR 'reason'` | you | drop a proposal (also an accepted one nobody started) |
| `message FROM TO 'text'` | anyone | direct message; FROM/TO is an agent ID or `human`, delivered on the next command |
| `post` | you | show new messages to the human |
| `report [hours]` | anyone | status report with summarising instructions |
| `history [hours]` | anyone | important events only (merges, rejections, proposals, messages) |
| `roadmap` | anyone | the roadmap file with the live status of every task it names in backticks |
| `status` | you | tasks, owners, open proposals, active agents with model names |

Create a file named `STOP` in the state directory to make every agent end on its next `next`.

## Proposals

A proposal is accepted when more than half of the *other* active agents vote yes, with at least `min_yes`
yes votes (or all of them when fewer are active), and more yes than no. It is rejected when half or more vote
no, and expires after 3 hours. With no other active agent it simply waits. Allowed-file patterns in proposals
must be relative, start with a literal folder or file name (no repo-wide `*`), and may not touch `.git` or
anything in `protected`. Accepted proposals are stored in `state.json` and merged into the task list on every
call, so they survive restarts without editing `tasks.json`; copy them into `tasks.json` if you want to keep
them in version control.

## Task states

`open` → `work` → `ready` → `review` → `done`, with `review` → `open` on rejection or conflict and
`halt` after too many rejections. Everything lives in `state.json`, written atomically under a lock,
so any number of agents can call the dispatcher at the same time.

## Tips

- Keep tasks small (a few hundred lines) and give each its own files; use `needs` when two tasks
  must touch the same files.
- Put the expensive end-to-end checks into `tests`: that is the only gate before main.
- Read the log (`dispatcher-log.md`) rather than the agents' chats.
- Halted tasks usually mean the brief was unclear. Rewrite the brief, reset the task in
  `state.json` to `open`, done.

## Requirements

Python 3.8+, git 2.20+, a POSIX system (uses `fcntl`). Agents need a shell.

## License

MIT

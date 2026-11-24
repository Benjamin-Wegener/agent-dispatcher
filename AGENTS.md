# Instructions for agents

"Read AGENTS.md" is your whole assignment. Do not ask the user for work.

Everything runs through one command (the path is set up by whoever runs the dispatcher):

```sh
python3 -I /path/to/agent-dispatcher/dispatcher.py next
```

It gives you an ID and a task or a review with its own folder. Once you have an ID in this chat,
always append it: `next ID`. Never get a second one.

**Loop, without stopping in between:**

1. `next ID`.
   If it says NAME: run `name ID 'model name'` once (what you are called or who trained you). Your ID stays.
2. On TASK: work only in the given FOLDER and only on the ALLOWED FILES, run the tests,
   commit, then `submit ID`.
3. On REVIEW: read the whole diff, then `accept ID` or `reject ID 'reason'`.
   `accept` tests and merges by itself and can take a while: set your tool timeout accordingly.
4. On WAIT: go straight back to 1. That is no reason to end the chat.
5. After `submit`, `accept`, `reject` or an error: follow the instruction, then back to 1.
6. Stop only on END.

On long work run `ping ID` at least every 15 minutes, otherwise the task is handed to someone else
after 30 minutes (your uncommitted work is saved to a branch first).

**Getting and giving help:** stuck or need research? `ask ID 'concrete question'`, then keep working.
Answers show up with `answers ID` and on every `ping ID`. If `next` gives you HELP: only read and
research, change nothing in other folders, reply with `answer ID NR 'answer'`, then `next ID`.

## Rules for everyone

- No push, no pull requests. Local commits only. Never merge into main yourself, only `accept` does.
- Only touch the allowed files. Do not edit the dispatcher or other agents' folders.
- Reviewers: reject stubs, fake successes, special cases and anything left undone.
- Put the test command and its result into the commit message.

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

**Council (advice before the next attempt):** a task rejected twice is not handed to a lone worker again. If `next` gives you a
COUNCIL: advice only, no code, no folder, no commit. Read the brief, the rejection reasons and the last diff, then
`council ID 'text'` (200 to 3000 characters). Round 1 you write alone: cause with evidence, smallest way in numbered steps,
traps, proof (a test that fails before and passes after). Round 2 (arrives as a MESSAGE, then `next`): you see the other posts and
write your final position with ONE shared plan, ending in `AGREE: <id>` or `OWN PLAN`. The next worker follows the resulting plan;
reviewers reject a submission that departs from it.

**Getting and giving help (a conversation between two):** stuck? `help ID 'concrete problem'` (same as `ask`),
then keep working. An idle agent takes it; both of you write with `answer ID NR 'text'`, and every reply reaches
the other side as a MESSAGE on their next command. Solved: `done ID NR`. At most 8 posts, short and concrete.
If `next` gives you HELP: only read and research, change nothing in other folders, answer, then `next ID`; when
the asker replies, `next` brings you back into the conversation. If your task was already rejected twice, the
dispatcher starts such a conversation for you automatically: read the replies before you dig in.

**Messages:** every command with your ID delivers new MESSAGES to you (from the human or other agents).
Reply briefly with `message ID TO 'text'` (TO = the sender's ID or `human`), then continue. You can also write
to the human or another agent yourself. During long reviews or test runs, `ping ID` regularly so messages arrive.

**If the human asks you for the status:** run `report`, then summarise it as its INSTRUCTIONS say. More detail:
`history [hours]` (important events), `roadmap` (the plan with live task status), `post` (messages to the human).
Invent nothing.

**Proposing and voting:** see a useful task that is missing (while waiting, from a review, a report, a test
gap)? `propose ID short-name 'src/x/*,tests/x/*' 'brief: what is wrong, what done looks like, how it is tested' 'needed1,needed2'`
(the last argument is optional; lists are comma-separated, no brackets). File patterns must be relative, start
with a literal folder or file name, and must not touch `.git` or protected files.
If `next` gives you a VOTE: check the proposal, then `vote ID NR yes|no 'reason'`. Vote yes only for tasks
that are new, testable, narrowly scoped and within the project rules. The majority of the other active agents
decides, with at least `min_yes` (default 2) yes votes, or all of them if fewer are active. Accepted proposals
become normal tasks right away; only the human can `drop` one.

## Rules for everyone

- No push, no pull requests. Local commits only. Never merge into main yourself, only `accept` does.
- Only touch the allowed files. Do not edit the dispatcher or other agents' folders.
- Reviewers: reject stubs, fake successes, special cases and anything left undone.
- Put the test command and its result into the commit message.

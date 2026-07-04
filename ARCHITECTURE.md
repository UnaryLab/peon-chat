# peon architecture

Internals and design of the peon process: how the package is laid out, how
the two CLI backends are abstracted, the exact verified CLI invocations, how
independent per-thread contexts are guaranteed, and how the async, non-blocking
message handling works.

See the [README](README.md) for installation and usage.

## Layout

```
peon/                          project root
  agents.json                  the agent definitions (SINGLE SOURCE OF TRUTH)
  quotes.json                  optional placeholder quotes (missing/invalid = default "thinking..." text)
  src/                         the importable package (run as `python -m src`)
    __init__.py
    __main__.py                `python -m src` runs the app; `python -m src manifest <name>` prints a manifest
    agents.py                  loads + validates agents.json into REGISTRY (duplicate names are a ValueError); token env-var names
    env.py                     load_env(): loads .env into os.environ with override=True (runs first)
    manifest.py                build_manifest(agent) -> the Slack app manifest dict
    app.py                     FACADE over src/slack/ (re-exports main/reconcile/build_app_for + the test patch surface)
    store/                     vendor-NEUTRAL persistence package (no slack_bolt, no runner deps)
      __init__.py              public store surface used by app + both runners
      base.py                  single source of truth: shared lock (_SESSIONS_LOCK), path resolution (_sessions_path/_sibling_store_path), dict + list load/save, _resolve_path seam
      sessions.py              sessions.json: (agent, thread) -> session_id (get/set/clear/get_or_create)
      overrides.py             overrides.json: (agent, thread) -> {model?, effort?}
      crons.py                 crons.json: list of cron entries (add/list/remove/set_enabled)
      jobs.py                  jobs.json: list of background-job entries (add/list/remove)
      workdir.py               get_workdir: per-thread workdir path scheme (the run's cwd)
    runners/                   the runner subpackage
      __init__.py              get_runner(backend) -> the runner facade module (claude or codex); the answer-seam contract
      claude.py                Claude-only runner internals: argv (build_command), run_claude, streaming, answer
      codex.py                 Codex-only runner internals: argv (build_command), run_codex, streaming, answer (NO claude/claude_runner import)
      common.py                cross-vendor shared: seen_before (dedup) + Interrupt (run cancel token) + _stream_enabled/_cwd_from_overrides (runtime helpers) + safe_on_update/format_process_failure + drain_stderr (concurrent stderr drain) + _int_env (tolerant int env parse)
      claude_runner.py         FACADE re-exporting src/runners/claude.py + common.{seen_before, Interrupt} + src/store/* (back-compat public seam)
      codex_runner.py          FACADE re-exporting src/runners/codex.py
    slack/                     Slack-facing layer (the only place that imports slack_bolt)
      __init__.py              package note; app.py facade re-exports from here
      app.py                   Bolt + Socket Mode build/reconcile/main + signal handling (one App per agent, one process)
      handlers.py              mention/message dispatch: _handle, _run_and_update, the streaming updater
      control.py               !model/!effort/!reset/!new/!cron/!job + !stop interrupt dispatcher (CONTROL_RE)
      interrupt.py             !stop run-interrupt registry + phrase matcher (per-thread Interrupt tokens)
      scheduler.py             in-process cron loop (_scheduler_tick) + cron_matches
      files.py                 attachment download (inbound) / upload (outbound)
      jobs.py                  <<job:>> marker, detached spawn, completion watcher, restart re-attach, !job list/kill
      usage.py                 _format_usage / _usage_enabled (SHOW_USAGE footer)
      quotes.py                random_quote(): placeholder quotes from quotes.json (mtime-cached, graceful)
  tests/
    test_*.py                  themed pytest suites sharing tests/helpers.py. No live Slack/Claude/Codex calls
  requirements.txt
  .env.example                 credentials + optional config
  README.md
```

`src/agents.py`, `src/manifest.py`, the `src/store/` package, and the
`src/runners/` subpackage do **not** import `slack_bolt`, so the self-check and
`python -m src manifest <name>` run even without Slack installed. Only the
`src/slack/` package and the `src/app.py` facade pull in Bolt. Intra-package
imports are relative, so the package directory name is not hardcoded in the code.

## Facades and the test seam

Three module paths are thin **facades** over the split-out implementation:
`src/runners/claude_runner.py` (re-exports `src/runners/claude.py` +
`common.{seen_before, Interrupt}` + the whole `src/store/*` surface), `src/runners/codex_runner.py`
(re-exports `src/runners/codex.py`), and `src/app.py` (re-exports `src/slack/*`).
The split is behavior-preserving: the verified CLI invocations and the public
seam are unchanged.

The facades exist because the test suite references symbols and monkeypatches
attributes on those EXACT module paths (`src.runners.claude_runner` /
`src.runners.codex_runner` / `src.app`), and `get_runner` returns the facade
objects (`get_runner("claude") is claude_runner`). Two kinds of patch target must
keep working:

- **Kind-A, shared-singleton attributes.** A facade re-imports the module objects
  that tests patch so the patch and the implementation see the SAME object. In the
  runner facades that is `subprocess` (`subprocess.run`/`.Popen` is patched via
  `claude_runner.subprocess` / `codex_runner.subprocess`; the implementation
  module calls through its own bare `import subprocess`, the same process-wide
  module object). In the `app` facade it is `subprocess` (the detached
  background-job spawn, `app.subprocess.Popen`), `tempfile`, and the `runners`
  package object (`app.tempfile.gettempdir`, `app.runners.get_runner`).
- **Kind-B, lazy-facade-resolved names.** A set of function/class names is resolved
  by their cross-module callers THROUGH the facade via a lazy in-body
  `from src import app as _appfacade` (so a `setattr(app, name, ...)` in a test is
  honored at call time): `_run_and_update`, `_scheduler_tick`, `_fire_cron`,
  `build_app_for`, `SocketModeHandler`, `reconcile`, `_attachments_dir`,
  `_http_get_bytes`, and the background-job seams `_start_job` / `_watch_job` /
  `_finish_job` / `_pid_alive`. The store layer's `_resolve_path` seam (above) is
  the same idea for the store-path resolvers patched on `claude_runner`.

When adding a symbol to a store/slack/runner submodule that a test references on
one of these paths, re-export it from the relevant facade so the seam stays
complete.

## Backend abstraction

Each agent is backed by either the `claude` CLI or the `codex` CLI, selected by
its registry `backend` field (default `"claude"`). Both runner modules expose one
unified seam:

```python
answer(agent, prompt, prior_session_id, timeout=None, overrides=None, on_update=None, cancel=None, on_session=None)
    -> (reply_text, session_id_to_store, meta)
```

`prior_session_id` is the stored session id for this `(agent, thread)` key (or
`None` on the first message), and the returned `session_id_to_store` is whatever
the caller must persist for resumes. `overrides` is the per-thread
model/effort override dict (see [Per-thread stores](#per-thread-stores)); `on_update`
is an optional `on_update(partial_text)` callback for streaming; `cancel` is an
optional `Interrupt` token so a `!stop` can SIGINT the streaming subprocess (see
[Run interrupt](#run-interrupt-stop)); `on_session` is an optional
`on_session(session_id)` callback the caller uses to PERSIST the id at run START
(see [persist-at-start](#how-independent-contexts-are-guaranteed)); `meta` is the
usage dict `{context_pct, tokens, cost_usd, duration_s}` (any field `None`) that
backs the [usage footer](#telemetry-the-usage-footer-show_usage). This hides the
two backends' DIFFERENT session lifecycles behind one call shape:

- **claude** needs the session id up front: `None` -> mint a `uuid4` and run a
  new session (`--session-id`); otherwise `--resume` it. Returns the id it used.
  It fires `on_session` the instant a NEW id is minted, BEFORE the subprocess
  starts, so an interrupted run leaves a resumable id (see below).
- **codex** MINTS its own `thread_id`: `None` -> a fresh `codex exec` run, then
  the freshly-minted `thread_id` is parsed from stdout and returned so the caller
  can persist it; otherwise `codex exec resume <thread_id>` and the prior id is
  returned unchanged. It accepts `on_session` for seam symmetry but ignores it
  (the id is only known post-run; the caller's post-run persist covers codex).

`app.py` is backend-agnostic: it loads the prior id (`get_session`), calls
`runners.get_runner(agent.get("backend", "claude")).answer(...)`, then persists
the returned id (`set_session`). The session store (one JSON file + lock) lives in
the vendor-neutral `src/store/` package and is shared by both backends (the
`claude_runner` facade re-exports it for back-compat); the key is `(agent_name,
thread_ts)` for **every** backend, so contexts stay independent (see below).

## The verified claude invocation (per agent)

`build_command` (the logic lives in `src/runners/claude.py`, re-exported by the
`claude_runner` facade) produces exactly these argv lists (claude CLI 2.1.187,
all empirically verified to work):

The model and effort come from each agent's `agents.json` entry. The shipped
claude agents pin `"model": "claude-opus-4-8[1m]"`, so `--model claude-opus-4-8[1m]`
appears in every argv; if an entry omitted `model`, code falls back to that same
pin (and logs a warning). Reasoning effort is the entry's `effort` field (accepted
values `low`, `medium`, `high`, `xhigh`, `max`), which adds `--effort <level>`
after `--model`; an absent/empty `effort` means no flag (the CLI default), but
every shipped entry sets one (Aristotle `xhigh`; Brunel and Cicero `high`), so
the shipped argv always carries `--effort`. To change either, edit that agent's
`agents.json` entry (e.g. `"effort": "high"` or `"model": "claude-sonnet-4-6"`);
there is no env-var override:

```
# Aristotle, new thread:
claude -p --output-format json --session-id <uuid> --agent unarylab-research:research_manager --permission-mode bypassPermissions --model claude-opus-4-8[1m] --effort xhigh "<prompt>"
# Aristotle, continuing the same thread:
claude -p --output-format json --resume <uuid> --agent unarylab-research:research_manager --permission-mode bypassPermissions --model claude-opus-4-8[1m] --effort xhigh "<prompt>"

# Brunel, new / resume (same shape, different agent):
claude -p --output-format json --session-id <uuid> --agent unarylab-research:project_manager --permission-mode bypassPermissions --model claude-opus-4-8[1m] --effort high "<prompt>"
claude -p --output-format json --resume     <uuid> --agent unarylab-research:project_manager --permission-mode bypassPermissions --model claude-opus-4-8[1m] --effort high "<prompt>"

# Cicero (general/default run: NO --agent flag):
claude -p --output-format json --session-id <uuid> --permission-mode bypassPermissions --model claude-opus-4-8[1m] --effort high "<prompt>"
claude -p --output-format json --resume     <uuid> --permission-mode bypassPermissions --model claude-opus-4-8[1m] --effort high "<prompt>"
```

`--output-format json` makes stdout a single JSON object; we read the `result`
field for the reply and check `is_error` plus the exit code for failures.
`--permission-mode bypassPermissions` runs the agent fully unsandboxed (see
[Per-thread workdir](#per-thread-workdir)); it sits between `--agent` and
`--model` on both fresh and resume runs.

**The one argv switch (still exact, asserted in lockstep).** With `STREAM_OUTPUT`
on (the run-time default), `--output-format json` becomes `--output-format
stream-json --include-partial-messages --verbose` (in `-p` mode `stream-json`
REQUIRES `--verbose`). stdout is then JSONL; the terminal `result` event has the
SAME shape as the single-blob JSON, so meta parsing is shared. `STREAM_OUTPUT=0`
restores the exact argv above.

Note on `--agent` and `--resume`: we include `--agent` on **both** new and
resume runs when the agent has one. Resume was verified to work, and repeating
`--agent` on resume is harmless and consistent (it just re-asserts the same
persona), so we always include it to guarantee a resumed thread keeps its brain.

## The verified codex invocation (Codex-backed agents, e.g. Dijkstra)

`build_command` (the logic lives in `src/runners/codex.py`, re-exported by the
`codex_runner` facade) produces exactly these argv lists (codex-cli 0.141.0, all
empirically verified). Codex mints its own session id (a `thread_id`), so a fresh
run captures it from stdout; a resume passes it back.
The `--profile`, `-m`, and `-c model_reasoning_effort` flags are conditional on
the entry's fields; the shipped Dijkstra entry sets all three
(`codex_profile: project_manager`, `model: gpt-5.5`, `effort: high`):

```
# Dijkstra, fresh run (mints a thread_id; reply is written to the -o file):
codex exec --json --skip-git-repo-check -s danger-full-access --profile project_manager -o <last_message_file> -m gpt-5.5 -c model_reasoning_effort="high" "<prompt>"
# Dijkstra, continuing the same thread (resume by the captured thread_id; no --profile):
codex exec resume <thread_id> --json --skip-git-repo-check -c sandbox_mode=danger-full-access -o <last_message_file> -m gpt-5.5 -c model_reasoning_effort="high" "<prompt>"
```

Details:

- `--skip-git-repo-check` is **required**: the run's cwd (the per-thread
  workdir under `WORKDIR_BASE`) is not a git repo.
- The run is fully unsandboxed (see [Per-thread workdir](#per-thread-workdir)).
  On a fresh run that is `-s danger-full-access`; on a `resume` run (which does
  **not** accept `-s/--sandbox`) it is `-c sandbox_mode=danger-full-access` (raw
  enum string; do not TOML-quote it). The runner sets the subprocess cwd to the
  thread's workdir.
- Streaming: codex ALREADY emits JSONL via `--json`, so `STREAM_OUTPUT` changes
  only HOW stdout is consumed (line-by-line vs. all-at-once), never the argv. The
  `-o` file stays the authoritative final reply on both paths.
- `-o/--output-last-message <file>`: the agent's final reply text is written to
  this file (a temp file the runner creates and cleans up); stdout is JSONL whose
  first `{"type":"thread.started","thread_id":...}` event carries the minted
  `thread_id` we persist.
- The Codex model comes from the agent's `agents.json` `model` field (the shipped
  Dijkstra entry sets `"model": "gpt-5.5"`), which adds `-m <model>`. If the field
  is absent/empty, no `-m` is passed and Codex uses its own configured default (we
  deliberately do not hardcode a fallback model name).
- Reasoning effort is the entry's `effort` field (accepted values `none`,
  `minimal`, `low`, `medium`, `high`, `xhigh`, subject to the active Codex
  model). When set, it adds `-c model_reasoning_effort="<level>"` on both the
  fresh and resume runs; absent/empty means no override (the CLI/model default).
  As with claude, `agents.json` is the sole source of model/effort: no env-var
  override.
- Persona via `--profile <name>` (OPTIONAL, codex-only): when the agent's
  `agents.json` entry sets a non-empty `codex_profile` (a profile NAME), the fresh
  run appends `--profile <name>`, so codex layers `~/.codex/<name>.config.toml`
  (whose `developer_instructions` is the persona) on top of the base config. This
  is the codex analog of claude's `--agent`: both name an operator-installed
  persona installed outside this repo. The CLI flags (`-m`, `-c
  model_reasoning_effort`) override profile config, so `agents.json` stays
  authoritative for model/effort. The flag is applied on the **fresh** `codex exec`
  run only: `codex exec resume` does not accept `--profile` (verified against
  codex-cli 0.142.0), and the resumed thread already carries the persona from turn
  one. A missing profile makes codex itself error, surfaced by `run_codex`'s
  existing error handling. The native `.codex/agents` subagent-spawn path does NOT
  apply a persona under headless `codex exec` on this version, which is why the
  profile approach is used.
- Codex has no namespaced-subagent concept the way claude does, so Dijkstra is a
  plain general Codex run (no `--agent`).

## How independent contexts are guaranteed

Context is keyed on `(agent_name, conversation_key)`, where the key is the
Slack thread ts, or the DM channel id for a flat 1:1 DM:

- For a top-level mention (not already in a thread), the message's own `ts` is
  used as the thread root.
- For a top-level message in a 1:1 DM (`im` channel), the DM CHANNEL id is the
  key: one rolling conversation per DM, not one per message. Threads inside a
  DM key on their thread ts like any other thread.
- A persistent JSON map `sessions.json` stores
  `{ "<agent_name>:<thread_ts>": "<uuid session id>" }`.
- First message for a key -> mint a fresh `uuid4`, store it, run with
  `--session-id` (new session). Subsequent messages for that key -> run with
  `--resume` (continue).

Because the key **includes `agent_name`**, different agents never share a
session id even for the same `thread_ts`, so contexts stay independent. A new
Slack thread is a new key, hence a fresh context. (See the
`get_or_create_session` tests in `tests/test_build_command.py` and
`tests/test_env.py` for the guarantee.)

**Persist-at-start (resilient to interruption).** peon runs under launchd with
`KeepAlive=true`, so a machine sleep / network drop / crash relaunches peon and
kills the in-flight `claude` child. claude's id is minted up front, so the worker
passes an `on_session` callback that `set_session`s the id the INSTANT it is
minted, BEFORE the subprocess runs (`claude.answer`), not only after a clean run.
So an interrupted run leaves a resumable id and the next mention `--resume`s the
half-written session instead of starting over. (codex can't pre-persist: its id is
born from the run's stdout; an interrupted fresh codex run is the one case that may
restart.)

**Dead-session self-heal.** If a stored id points at a session claude no longer
has, `--resume <id>` fails with `No conversation found with session ID: <id>`.
`claude.answer` detects exactly that error, mints+persists a fresh id (clearing the
dead one) and retries ONCE as a new session, so a stale id can never wedge a thread
forever. The marker is checked against the FULL raw stderr carried on the error
object (`err.stderr`), not just the formatted message (which truncates stderr to
1000 chars), so noisy stderr ahead of the marker cannot defeat the heal.

The session store is a plain JSON file: fine for one process at modest volume.
The background worker threads share the process, so the read-modify-write is
guarded by a `threading.Lock`; swap for sqlite if it grows or needs concurrency
across processes (marked with a `# ponytail:` comment in the code). Every save
is atomic (write to a `.tmp` sibling, then `os.replace`), so a hard kill
mid-write can never leave a truncated store behind (the tolerant loaders would
silently read truncated JSON as empty, wiping every session).

### Per-thread stores

`sessions.json` is one of FOUR per-thread JSON stores, all owned by the
vendor-neutral `src/store/` package (`store.base` is the single source of truth
for the shared lock `_SESSIONS_LOCK` and the path resolution `_sessions_path` /
`_sibling_store_path`; `sessions.py` / `overrides.py` / `crons.py` / `jobs.py` /
`workdir.py` are the per-store modules). The `claude_runner`
facade re-exports all of them for back-compat. They all sit beside
`sessions.json` via `_sibling_store_path(<name>)`, so the single `SESSIONS_PATH`
env var redirects every store at once (no per-store env var). The dict-shaped
stores share `_load_dict_store`/`_save_dict_store`; the list-shaped cron and job
stores share `_load_list_store`/`_save_list_store` (both pairs in store.base).
All four write atomically (temp file + `os.replace`, see above):

- **`sessions.json`** (dict): `(agent, thread) -> session_id`, above. The `!new`
  control phrase clears one key (`clear_session`), so the next message in that
  conversation starts a fresh CLI context.
- **`overrides.json`** (dict): `(agent, thread) -> {model?, effort?}`. Set by the
  `!model`/`!effort`/`!reset` control phrases.
- **`crons.json`** (list): `{id, schedule, agent, channel, thread_ts, prompt,
  enabled}` entries (see [Cron](#cron-slack-native-in-process)).
- **`jobs.json`** (list): `{id, agent, channel, thread_ts, pid, logfile, cmd,
  started_ts}` background-job entries (see
  [Background jobs](#background-jobs-job-detached));
  `thread_ts` is the conversation KEY, so it may be a DM channel id. A persisted
  entry means "still running (or awaiting re-attach after a restart)"; the
  completion watcher removes it.

When a store fn is called with `path=None`, `store.base._resolve_path(attr,
fallback)` resolves the JSON path through the LIVE `claude_runner.<attr>` (e.g.
`_sessions_path` / `_overrides_path` / `_crons_path` / `_jobs_path`), falling back to the store's
own local resolver. In production `claude_runner.<attr>` IS that local resolver
(re-exported), so it is behavior-identical; the seam exists so a test that does
`setattr(claude_runner, "_sessions_path", ...)` to redirect `SESSIONS_PATH` is
honored even though the store code now lives in `src/store/`.

## Telemetry: the usage footer (`SHOW_USAGE`)

Each `runner.answer` returns a `meta` dict `{context_pct, tokens, cost_usd,
duration_s}` parsed from the SAME CLI output already read for the reply (no extra
CLI call). For claude it comes from the result payload's `usage` /
`total_cost_usd` / `duration_ms`; `context_pct` is the input-side tokens over the
model's window (1M for a `[1m]` model id, else 200k). For codex `cost_usd` and
`context_pct` are always `None` (no cost field, unknown window); `tokens` come
from token-usage events and `duration_s` is wall-clock. When `SHOW_USAGE` is
truthy, `app._format_usage(meta)` renders a one-line `· N% · X tok · $Y · Zs`
footer under the reply, dropping any `None` field; an all-`None` meta yields no
footer. Default ON; read live, so a SIGHUP `.env` reload toggles it.

## Streaming (`STREAM_OUTPUT`)

Default ON. Both runners read the CLI's JSONL stdout incrementally and call the
`on_update(partial_text)` seam; `app._make_stream_updater` throttles
`chat_update` to ~1/sec (the first chunk always posts), and the worker does an
unconditional FINAL `chat_update` with the complete text plus footer, so the last
token is never dropped. The authoritative final reply is still the CLI's terminal
result (claude's `result` event / codex's `-o` file), so the streamed text only
drives live updates. `STREAM_OUTPUT=0` is the legacy single-shot path (one final
update, claude's exact pre-streaming argv). See the claude/codex argv notes above
for the argv impact (claude: streaming flags; codex: none).

On the streaming path each runner drains the CLI's PIPE'd stderr on a concurrent
daemon thread (`drain_stderr` in `src/runners/common.py`): a CLI writing more
than the OS pipe buffer (~64KB) of stderr would otherwise block on the write and
stop producing stdout, deadlocking the readline loop. The drained text still
feeds the error message on a nonzero exit. On both paths CLI output is decoded
with `errors="replace"`, so a stray non-UTF8 byte degrades to a replacement
character instead of raising through as an unexpected error.

## Control phrases (one dispatcher)

`app._handle_control_phrase` is the single parser/dispatcher: it matches
`CONTROL_RE` (`^!(model|effort|reset|new|cron|job)\b ...`, compiled with DOTALL so a
multi-line phrase -- e.g. a `!cron add` whose prompt spans lines -- still
matches instead of falling through to the help ack) on the de-mentioned
prompt and routes to the right handler. A handled phrase acks into the thread and
does NOT run the agent (the agent runs only for a non-`!` message). `!model
<id>` / `!effort <level>` / `!reset` mutate `overrides.json`; `!new` clears the
conversation's `sessions.json` key (`clear_session`) so the next message starts a
fresh CLI context (overrides/crons/workdir untouched; declined with a busy ack
while a run is in flight for the key, since the worker's end-of-run `set_session`
would resurrect the cleared id -- checked read-only via `interrupt.is_running`);
`!cron add|list|remove|on|off` mutates `crons.json`; `!job list|kill <id>`
(`jobs._handle_job_command`) manages the DISPATCHING agent's background jobs,
scoped to that agent across ALL conversations (another agent's job id reads as
"no such job"): `list` reads `jobs.json` (one line per job: id, conversation
key, pid, command ellipsized at `_JOB_LIST_CMD_CHARS`), and `kill <id>` SIGTERMs
the job's whole process group (`os.killpg`; the spawn's `start_new_session`
makes the pid a group leader) WITHOUT touching the `jobs.json` entry -- the
watcher owns delivery + removal, so a kill settles through the normal completion
flow (an unsignalable group -- gone, a pid recycled to another uid, or a
malformed pid -- acks as already finished). Ahead of the `!`-gate the dispatcher
also matches the interrupt phrases (`!stop` / bare `stop` / `ctrl-c` / `^c` /
`interrupt`) and signals the thread's in-flight run (see
[Run interrupt](#run-interrupt-stop)).

## Run interrupt (`!stop`)

A `!stop` (or bare `stop` / `interrupt` / `ctrl-c` / `^c` / `/interrupt`) in a
thread is the Slack analog of a terminal Ctrl-C: it interrupts the run in flight
for that `(agent, thread)` without starting a new one. It is matched in
`_handle_control_phrase` BEFORE the `!`-gate (the bare forms carry no `!`).

- **`Interrupt` token (`src/runners/common.py`).** A one-shot, thread-safe cancel
  handle for the live `Popen`, which the runner attaches via `.arm(proc)` right
  after spawning. `arm()` and `.request()` share one lock, so a `!stop` that lands
  BEFORE the spawn finishes (flag set, proc still `None`) is delivered the moment
  the proc is attached, instead of being acked and silently lost. `.request()`
  sets a flag and sends **SIGINT** to a live process (mimics Ctrl-C, letting the
  CLI flush its own session state); if the process has ALREADY exited but its
  stdout is still open (an orphaned descendant inherited the write end, so the
  reader never sees EOF), it closes the read end instead, unblocking the wedged
  reader, which then settles as an interrupted partial.
- **Registry (`src/slack/interrupt.py`).** An in-memory `{(agent, thread):
  Interrupt}` (single-process, like the dedup). It doubles as the per-thread
  **busy guard**: `_handle` atomically claims the slot with `try_register`
  BEFORE spawning the worker, and a second message while a run is in flight
  gets `None` back and is declined with a short "still working" reply (never
  two concurrent runs `--resume`-ing one session id). The cron path claims the
  slot the SAME way (`try_register` inside `_run_and_update`): a fire that lands
  while a run is in flight is skipped, its placeholder updated to a "skipped: a
  run is already in progress" note; the unconditional last-writer-wins `register`
  remains only as the raw primitive. The worker always `unregister`s the token in
  a `finally`; `!stop` calls `request(agent, thread)`, which returns whether a
  run was live.
- **Graceful settle (the runners).** The worker passes the token into
  `answer(..., cancel=token)`; each runner arms it with the live `Popen`. On the
  SIGINT-induced nonzero exit, the streaming loop checks `cancel.requested` and
  RETURNS the partial reply instead of raising. So `set_session` still persists the
  session id and **the thread stays resumable**; the worker marks the reply
  `_(interrupted)_`.

Session preservation on a mid-run kill differs per backend: claude's id is known up
front (always resumable); codex salvages its `thread_id` from the partial stream
(resumable unless interrupted in the first few ms of a fresh run, before
`thread.started` is emitted, which falls back to a clean interrupted notice).

**Streaming only.** Only the `Popen` streaming path is interruptible; under
`STREAM_OUTPUT=0` the worker blocks in `subprocess.run` with no handle, so `.proc`
stays `None` and `!stop` is a no-op (the run finishes or times out on its own).

## Per-thread workdir

**SECURITY: every run is fully unsandboxed.** The runners always emit claude
`--permission-mode bypassPermissions` and codex `-s danger-full-access` (fresh) /
`-c sandbox_mode=danger-full-access` (resume), so an agent can read/write any path
and run any command. Anyone who can DM/mention a bot can run arbitrary commands as
the operator; this is a deliberate personal/lab tradeoff, so restrict who can
reach the bots.

Each run gets a per-thread workdir as its cwd. The worker injects
`_workdir = get_workdir(agent, thread)` into `overrides`, and both runners set the
subprocess cwd to it. `get_workdir` builds the path under `WORKDIR_BASE` (default
`~/Projects/.peon-workdirs`), namespaced by agent + thread, and creates it on
demand. The default base is an **absolute path OUTSIDE this repo** so a run's
default cwd is never the framework source; `get_workdir` always returns an
ABSOLUTE path (the subprocess cwd needs one), so the per-thread workdir lives at
`<home>/Projects/.peon-workdirs/<agent>/<thread>`. Set `WORKDIR_BASE` to override.

The workdir is the run's cwd/home, not a confinement boundary (the run is
unsandboxed). Its purpose is the outbound file flow: a run delivers files only by
naming them in a `<<files: ...>>` marker, and each named file is resolved inside
the workdir before upload. `get_workdir` is the single owner of the path scheme,
reused by both runners and by the outbound file upload.

## Files in and out

Inbound: a message's `files[]` are downloaded with the bot token
(`_http_get_bytes`, stdlib `urllib` with a 60s per-download timeout so a stalled
CDN read cannot wedge the thread's busy slot; the single mocked HTTP seam) into a
per-thread temp dir, and their local paths are appended to the prompt so the CLI
can open them. A threaded follow-up carrying an upload arrives with the
`file_share` message subtype, which the handler lets through (every other
subtype is ignored), so inbound files work on unmentioned follow-ups too.
Outbound is opt-in and model-driven: a run delivers files only by
ending its reply with a `<<files: name1, name2>>` marker (which is stripped from
the posted reply); the marker is recognized ONLY at the very end of the reply --
a reply that merely mentions the syntax mid-text is plain prose and triggers
nothing. Each named file is resolved inside the thread's workdir (paths
escaping it are rejected) and uploaded via `files_upload_v2`. Both need the
`files:read` / `files:write` bot scopes. No marker (the default), or no workdir
resolved for the thread, uploads nothing.

## Background jobs (`<<job:>>`, detached)

A run's turn is a single non-interactive CLI process: anything it backgrounds
dies with it. The sanctioned escape hatch is the trailing
`<<job: <shell command>>>` marker (src/slack/jobs.py), following the SAME
trailing-only rule as `<<files:>>` (a mid-text mention is plain prose; the
per-turn preamble tells the agent to emit it only when the user explicitly asks
for long/background work). The two markers use DIFFERENT body patterns: the
files pair keeps the tempered-dot helper (`files._trailing_marker_res`, body
cannot contain `>>`), while the job marker owns its own GREEDY pair in
src/slack/jobs.py so a shell command CAN contain `>>` (append redirects,
heredocs). Three rules guard the body. The job opener must START A LINE
(`(?:^|\n)[ \t]*` in both the parse and strip regexes): a mid-line prose
mention never anchors or strips, even when the reply happens to end in `>>`.
The parse anchors at the LAST line-start `<<job:` occurrence and requires the
closing `>>` at the very end of the reply. And the OPENER LINE discriminates
single-line from multi-line bodies: if the marker's opening line itself
contains a `>>`, the marker must be SINGLE-LINE and its closing `>>` must end
the reply, so a line-start quoted example (`<<job: make all>>` followed by
more prose in a reply that happens to end in `>>`) is prose, never shell; only
an opener line with NO `>>` may span newlines (multi-line heredoc commands
work, greedy with DOTALL to the reply-final `>>`). The one limitation this
buys: a multi-line body whose FIRST line contains `>>` is prose; put `>>`
appends on a single-line marker or on later lines of a heredoc. A command
ending in `>` works too (a reply ending `...>>>`: the final two `>` close the
marker, the rest stays in the command). The one carve-out: the job body may
not contain another marker opener (`<<job:` / `<<files:`), so a trailing
files marker after a mid-text job mention is never swallowed.

**Marker order (deterministic, tested).** In `_run_and_update` the JOB marker is
parsed FIRST, then the FILES marker, both BEFORE the 39,000-char Slack cap. So a
reply ending `<<files: a>>` then `<<job: cmd>>` (job line LAST) triggers both;
with the lines the other way around the files strip leaves the job marker
mid-text, i.e. plain prose. Both markers are also scrubbed from streamed
partials so neither flashes mid-stream.

**Spawn (`_start_job`).** After the final reply posts (never before: a spawn
failure must not clobber the reply; it surfaces as a warning note in the
thread), the worker resolves `_start_job` through the app facade and spawns
`/bin/sh -c <cmd>` with `start_new_session=True` (its own session, so it
survives the turn AND this process), `stdin=DEVNULL`, and stdout+stderr into
`job-<id>.log` inside the thread workdir (also the cwd). The command runs fully
unsandboxed like every run in peon. The entry `{id, agent, channel, thread_ts,
pid, logfile, cmd, started_ts}` is persisted to `jobs.json` (thread_ts is the
conversation KEY, possibly a DM channel id; `started_ts` is the epoch-seconds
spawn time the timeout window runs from, stamped inside `store.add_job`), and a
daemon watcher thread is armed. If persisting/arming fails AFTER the spawn, the
process GROUP is SIGKILLed and reaped (the job is a session/group leader, so a
child it already forked dies too) and no untracked orphan runs while the user
is told the job failed to start. An interrupted (`!stop`) run never spawns its job
(the worker gates the spawn on the cancel token).

**Concurrency limit (`JOB_MAX_CONCURRENT`).** The limit is GLOBAL across all
agents (it protects the machine): `_start_job` reads the env var LIVE per spawn
(default `_JOB_MAX_CONCURRENT_DEFAULT` = 4; `0` disables) and counts ALL
persisted `jobs.json` entries, running or awaiting delivery; an entry parked by
`_reattach_jobs` for a not-live agent holds a slot until that agent returns or
the operator hand-edits the store. At the limit the spawn is DECLINED, never
queued: no process left running, no entry, no watcher, and a "job not started:
N jobs already running (limit N); use `!job list` / `!job kill <id>`" note
posted into the reply thread (via the usual `_reply_thread_ts` translation; the
agent's reply itself still posts normally with the marker stripped). Race-free
by construction: the count-check and the append are ONE critical section inside
`store.add_job` (the shared store lock), so two simultaneous spawns serialize
there and can never both squeeze past the limit. Ordering wrinkle: the pid only
exists after Popen, so the process is spawned FIRST and on a decline the fresh
process GROUP is SIGKILLed and reaped (never a bare `proc.kill()`, which would
orphan a child the group leader already forked); this was chosen over reserving
a placeholder-pid entry
before the spawn because it is simpler and equally race-free (the persisted
entry count never exceeds the limit, and no pid-less reservation entry can ever
reach `!job list` or `_reattach_jobs`).

**Watcher + completion (`_watch_job` / `_finish_job`).** A watcher for a job
spawned this process lifetime just `proc.wait()`s (real exit code); a
RE-ATTACHED watcher polls the persisted pid with `os.kill(pid, 0)` every 5s
until gone (exit code unknown; the probe accepts the pid-reuse race, see the
`ponytail:` note in `_pid_alive`; a malformed pid reads as dead so the entry
still clears).

**Timeout (`AGENT_TIMEOUT_MIN`).** Both watcher paths enforce a max job lifetime
from the SAME `AGENT_TIMEOUT_MIN` knob that bounds a runner's subprocess (name
and default single-sourced in `common.agent_timeout_min`: minutes, default 2880,
`0` disables). The LIVE env
value is captured when the watcher (re)starts and is the enforced window for
that watcher's whole lifetime, so a SIGHUP `.env` reload applies to jobs
spawned or re-attached after it. The window runs from the entry's `started_ts`:
the this-lifetime path swaps the unbounded `proc.wait()` for a bounded
`proc.wait(timeout=remaining)`, and the re-attach poll checks the deadline each
pass, so a RE-ATTACHED watcher enforces only the REMAINING window (not a fresh
full one) and an already-expired re-attached job is killed on the FIRST poll.
An entry persisted before `started_ts` existed is treated as started now (never
retro-killed on sight). On expiry the watcher SIGTERMs the job's process group,
waits `_JOB_KILL_GRACE_S` (5s), SIGKILLs the group if still alive, then
delivers through the NORMAL completion flow below with a "timed out after N
min" label in the completion prompt/note (log tail still included).

On completion `_finish_job` reads the log TAIL
(`_JOB_LOG_TAIL_CHARS` = 4000 chars, seek-based, the ONE place that number
lives) and delivers the result mirroring a cron fire: it claims the thread's
busy slot via `try_register`, posts a placeholder, and synthesizes a follow-up
agent turn through the SAME `_run_and_update` seam with the prompt
`[background job finished, exit code N] output tail:\n...`, so the thread's
existing session resumes and the agent summarizes in context. A thread that is
BUSY at completion time is NOT skipped silently: the raw completion note (exit
code + tail) is posted as a plain message instead. Every post translates the
conversation key via `_reply_thread_ts` (a flat-DM key posts flat). The
`jobs.json` entry is removed only AFTER delivery lands (note posted, or
placeholder posted); a failed delivery keeps the entry so the next restart
re-attaches and re-delivers, and a crash between the post and the remove means
a rare double-delivery rather than a silently lost result. Security note: the
log tail is fed back as an agent-turn prompt, so external job output reaches a
fully unsandboxed agent turn automatically; consistent with the documented
posture above, but worth stating.

**Restart re-attach (`_reattach_jobs`).** `main()` calls it once after the live
handler set is built: for each persisted entry, an agent gone from the registry
drops the entry (warning); an agent in the registry but not LIVE (no Slack
connection to deliver through) leaves the entry for a later restart; otherwise
a re-attach watcher is spawned (`_watch_job` with no proc: the pid poll, so an
already-dead job runs the completion flow immediately, exit code unknown).
Crash-safe: a broken store or one bad entry never blocks startup.

**Control phrases (`!job list` / `!job kill <id>`).** `jobs._handle_job_command`
(dispatched by `_handle_control_phrase`, agent-scoped) lists the agent's jobs
or SIGTERMs a job's process group; the kill does NOT remove the `jobs.json`
entry, so it settles through the watcher's normal completion path above (the
terminated job's exit + log tail are delivered like any other completion). See
[Control phrases](#control-phrases-one-dispatcher).

## Cron (Slack-native, in-process)

A daemon thread (`_scheduler_loop`, started from `main()`) evaluates each minute
once, sleeping to the NEXT minute boundary rather than a fixed 60s (a fixed sleep
would drift forward each tick until a minute was skipped entirely). Each pass
re-reads `crons.json` (so a SIGHUP edit is picked up with no extra wiring) and
fires every ENABLED entry whose 5-field expression matches the current minute
(`cron_matches`, a hand-rolled matcher: `*`, lists, `A-B` ranges, `*/S` and
`A-B/S` steps, plus Vixie `N/S` = N through the field max in steps of S, e.g.
minute `5/15` -> 5,20,35,50; no `croniter`/APScheduler dependency). Each fire
runs on its OWN daemon thread (`_scheduler_tick` spawns one per fire), so a long
cron run never blocks the tick loop's next minutes. A fire synthesizes a run
through the SAME `_run_and_update` seam as a live mention, posting into the cron's
target thread; it claims the thread's busy slot via `try_register`, so a fire
that lands while a run is already in flight in that thread is skipped (its
placeholder is updated to a "skipped: a run is already in progress" note). A
skip-by-minute guard prevents a double-fire within one minute.
This is distinct from Claude Code's own `/schedule` (cloud routines); this one
runs inside this always-on process and posts back into Slack.

## Async / non-blocking

A backend run can take seconds to minutes, so we never block the Slack ack:
the handler acks fast, posts a placeholder in the thread (a random line from
`quotes.json`, else "<agent> is thinking..."), runs the agent's backend in a
background `threading.Thread` (subprocess with a configurable timeout in
MINUTES, default 2880 (2 days) via `AGENT_TIMEOUT_MIN`, the ONE timeout knob
shared with the background-job watcher (name and default single-sourced in
`common.agent_timeout_min`); read ONCE at import by each runner (a change needs
a restart; the job watcher reads it live instead), converted to seconds, and `0`
disables the timeout (None is passed to the subprocess wait, on both the
non-stream `subprocess.run` and the streaming `proc.wait`); a malformed value
logs a warning and falls back to the
default instead of killing the process at import, see `_int_env`; on the
streaming path this bounds the post-stream
`proc.wait`, not the whole read), then `chat_update`s the placeholder with the
result. A finished reply longer than Slack's 40,000-char `chat_update` limit is
capped by `_truncate_for_slack` (the first 39,000 chars are kept and a
truncation note appended); the `<<files:>>` marker is parsed BEFORE the cap (so
file delivery survives) and the interrupted label / usage footer are appended
AFTER it (so they survive too). One run per (agent, thread) at a time: `_handle` claims the thread's
interrupt-registry slot before spawning the worker and declines a concurrent
message with a short busy note (see [Run interrupt](#run-interrupt-stop)).
Failures (nonzero exit, timeout, an error result, empty/malformed output) are
caught (`ClaudeRunError` / `CodexRunError`) and posted as a short error message
into the thread; if the run had already streamed partial text, that partial
reply is kept instead, with a note that it was cut off and any message resumes
the thread (the session id was persisted at run start). One bad run never
crashes the process.

A mention-bearing in-thread reply is delivered as BOTH an `app_mention` and a
`message.*` event, so `app_mention` owns mentions (`on_message` skips replies
that `<@>`-mention the bot) and a bounded in-memory idempotency guard dedups on
agent name + message id. The key is PER AGENT because the Slack message id is
identical across every agent's delivery of one message: each agent answers a
given message at most once, and two agents mentioned in one message BOTH answer.
`on_message` also skips a follow-up whose text OPENS with another user's mention
("@B what do you think?" is directed at that bot, whose own `app_mention` handles
it); a mid-text mention is still an ordinary follow-up for the agents already in
the thread. These checks rest on the bot's own user id, resolved via `auth_test`
and cached per app; a FAILED lookup is not cached, so the next event retries it.
Message subtypes (edits, deletes, joins) are ignored EXCEPT `file_share`: a user
message carrying an upload arrives with that subtype, so a threaded follow-up
that attaches a file still runs.
For unmentioned threaded replies, `on_message` dispatches only when this agent
already has a stored `(agent, thread)` session, so one agent's thread continuation
cannot wake unrelated agents in the same channel.
Before a normal run, `_handle` fetches a bounded `conversations.replies`
transcript, following the pagination cursor (replies arrive OLDEST-first; up to
6 pages of 200) and keeping the NEWEST 50 visible messages before the current
event -- the thread's tail, never the stale head of a long thread -- and prepends
it as "Visible Slack thread so far". This lets a newly mentioned agent read
another agent's Slack-visible output in the same thread without sharing hidden
CLI session state.

**1:1 DMs ("im" channels) are message.im-only.** Slack does NOT dispatch
`app_mention` in an im channel (mpim group DMs DO get it and stay on the normal
path), so `on_message` routes every im-channel message straight to `_handle`
BEFORE its thread/mention gates; `_handle`'s subtype/bot_id filtering and dedup
still apply. `_handle` then splits the CONVERSATION KEY from the POSTING
TARGET: a top-level DM message is keyed by the DM CHANNEL id (one rolling
conversation per DM, spanning messages and restarts), while a threaded message
inside a DM keeps the normal per-thread key. The key feeds every store,
override, workdir, and interrupt lookup; posting goes through
`_reply_thread_ts(key)` (in `handlers.py`, re-exported by the `app` facade): a
ts-shaped key posts into that thread, a channel-id key posts FLAT
(`thread_ts=None`, since Slack rejects a channel id as `thread_ts`). That
translation covers the handler acks, the placeholder, control-phrase acks, the
outbound `files_upload_v2` (whose workdir resolution still uses the raw key),
and a cron fire whose stored `thread_ts` is a DM channel id. Flat DMs skip the
`conversations.replies` transcript fetch: there is no thread to fetch, and the
transcript exists to let OTHER agents read a shared thread, which does not
apply in a 1:1 DM (the rolling per-channel session already carries the
context).

## Hot-reload reconcile mechanics

A `SIGHUP` makes the running process re-read `agents.json` + `.env` and reconcile
its live Slack connections in place. The reconcile acts on the **delta only**:

- **Added** (now startable, not running) -> a new connection is built and connected.
- **Removed** (running, now gone or missing a token) -> its connection is cleanly
  closed and dropped.
- **Changed** (its `agents.json` entry OR either resolved token changed) -> *only*
  that one connection is restarted.
- **Unchanged** -> its connection is **left completely untouched**, so live
  conversations on every agent you did not edit are never interrupted.

"Changed" is detected by a per-agent snapshot: the full `agents.json` entry plus
the two token values it connected with. Rotating a token in `.env`, or editing an
agent's `model`/`effort`/`claude_agent`/`backend`, restarts just that agent.

The signal handler itself does the minimum (it sets an event); the actual
reconcile runs on the main thread. If the new `agents.json` is missing or invalid
JSON, or any step of the reconcile throws, the reload is skipped: a warning is
logged and all running agents are left exactly as they were. A bad reload never
drops a live agent and never kills the process. POSIX only (macOS/Linux).

# peon

A single always-on Python process that runs several Slack bot personas, each
backed by a headless CLI call (`claude` OR `codex`), with INDEPENDENT
conversation contexts. **One Slack app per agent**: each persona is its own
Slack app with its own bot user and tokens, so a user just @-mentions that
agent's bot directly (no keyword routing). The one process serves all of them at
once. Runs on **Linux and macOS** alike.

The four default personas (each its own Slack app):

| Agent | Backend  | Persona (`--agent` / `--profile`)     | Slack display name |
|-------|----------|---------------------------------------|--------------------|
| Aristotle | `claude` | `unarylab-research:research_manager`  | Aristotle              |
| Brunel   | `claude` | `unarylab-research:project_manager`   | Brunel                |
| Cicero  | `claude` | _(none: general/default run)_         | Cicero               |
| Dijkstra  | `codex`  | `project_manager` codex profile       | Dijkstra               |

The agents are defined declaratively in **`agents.json`** at the project root,
the **single source of truth**. Each entry has `name`, `display_name`,
`backend` (`"claude"` or `"codex"`), `model`, and `effort`. Claude entries also
carry `claude_agent` (the namespaced `claude --agent` value, or `null` for a
general run). Codex entries may instead carry an OPTIONAL `codex_profile`, the
codex persona analog of `claude_agent`: the NAME of an operator-installed
`~/.codex/<name>.config.toml` profile (whose `developer_instructions` is the
persona), applied as `--profile <name>` on the fresh run. Absent means a plain
run, and each persona field is ignored on the other backend. Dijkstra ships
`codex_profile: project_manager`; that profile comes from the
`unarylab-codex-marketplace` (its `scripts/install_profiles.py` installs
`~/.codex/project_manager.config.toml`).
`app.py` dispatches to the right runner via `runners.get_runner(backend)`.
`src/agents.py` loads + validates `agents.json` into its `REGISTRY` at import
(duplicate agent `name`s are rejected at load).

Per-agent **model** and **effort** come SOLELY from the `model`/`effort` fields in
`agents.json` (the single source of truth, set on every shipped entry). There is
**no env-var override** for them: to change a model or effort, edit `agents.json`.
Valid values: claude `effort` is one of `low`, `medium`, `high`, `xhigh`, `max`;
codex `effort` is one of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`
(subject to the active model). If an entry omits a field, code applies a single
fallback (claude model -> `claude-opus-4-8[1m]`; everything else -> omitted) and
logs a warning.

Adding a fifth agent on either backend is a **one-entry change** in `agents.json`
plus its own Slack app + two env vars (see below).

For internals and design (layout, backend abstraction, the verified CLI
invocations, context isolation, and the async model), see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Installation

### Requirements

- **git** with access to the **private** GitHub repo. Because the repo is
  private, you need either an SSH key registered with your GitHub account or a
  personal access token (PAT) with `repo` scope.
- **conda** (miniconda or anaconda) on `PATH`, used to create the **Python 3.12**
  environment.
- The **`claude` CLI**, installed, authenticated, and on `PATH` (used by the
  claude-backed agents; needs the `unarylab-research` plugin available, see
  [Prerequisites](#prerequisites)).
- The **`codex` CLI**, installed, authenticated, and on `PATH` (used by the
  codex-backed agent; only required if a Codex-backed agent like Dijkstra is
  configured).

### Steps

1. **Clone the private repo** (private access via SSH key or PAT is required):
   ```sh
   git clone git@github.com:<your-org>/peon.git
   cd peon
   ```
2. **Create and activate the env** (Python 3.12):
   ```sh
   conda create -n peon python=3.12 -y
   conda activate peon
   ```
3. **Install dependencies:**
   ```sh
   pip install -r requirements.txt
   ```
4. **Configure credentials:** copy the example env file and fill it in:
   ```sh
   cp .env.example .env
   ```
   Fill in each app's Slack tokens (`SLACK_BOT_TOKEN_*` / `SLACK_APP_TOKEN_*`);
   see [Prerequisites](#prerequisites) for how to create the Slack apps and
   obtain the bot (`xoxb-...`) and app-level (`xapp-...`) tokens. Model and effort
   are NOT set here: each agent's `model`/`effort` live in `agents.json`.
5. **Run it:**
   ```sh
   conda run -n peon python -m src
   ```
   For a real always-on deployment (systemd / launchd / nohup), see
   [Running always-on](#running-always-on).

## Using the agents in Slack

Each agent is its own Slack bot that you address by name.

1. **Start a conversation.** In a channel or group (including a group DM),
   @-mention an agent with your question. There is no command or keyword prefix: your whole message,
   minus the mention, becomes the prompt and goes straight to that agent. The
   agent opens a reply thread under your message and answers there. You briefly
   see a placeholder note (a random worker quote from `quotes.json` at the
   project root, or "...is thinking..." when that file is absent/empty), which
   is edited into the answer, incrementally while the reply streams. (A reply
   too long for one Slack message is truncated to fit, with a note.)

   ```
   @Aristotle survey stochastic computing accelerators
   @Brunel review this build plan
   @Cicero what's the capital of France?
   ```
2. **Continue the conversation.** Reply inside that thread. You can @-mention the
   agent again or just type your follow-up without mentioning it; either way the
   agent remembers the earlier turns in that thread and keeps the context.
3. **DM an agent directly.** In an agent's 1:1 DM you just type, no mention
   needed (Slack never delivers mentions as such in a DM, and there is only you
   and the bot anyway). The whole DM is ONE rolling conversation: every
   top-level DM message continues the same context, across messages and
   restarts, and replies post directly in the DM (not in a thread). Starting a
   thread on a DM message opens a separate conversation, exactly like a thread
   anywhere else. If Slack shows "Sending messages to this app has been turned
   off", the app's Messages Tab is disabled: update the app from a regenerated
   manifest (which now enables it), or toggle it by hand under App Home > Show
   Tabs > Messages Tab (check "Allow users to send ... messages").
4. **Ask without typing a question.** If you @-mention an agent with no actual
   question, it replies asking what you would like to ask.
5. **Talk to several agents.** Mention different agents (Aristotle, Brunel,
   Cicero, Dijkstra) to bring in different ones; mention two agents in the same
   message and each answers (each still answers a given message only once). Each
   remembers its own
   conversation separately, even in the same thread, so they do not share hidden
   CLI memory with each other. When an agent is invoked in an existing Slack
   thread, the visible prior thread messages are included in its prompt, so it can
   read another agent's Slack-visible output in that same thread.
6. **Send and receive files.** Attach files to your message and the agent can
   read them (their paths are passed to the CLI); this works on unmentioned
   thread follow-ups too. The agent sends files back only
   when you explicitly ask it to: it then ends its reply with a `<<files: ...>>`
   marker naming them, that marker is stripped from the message, and just those
   files (from its per-thread workdir) are uploaded into the thread. Only a
   marker at the very end of the reply counts (merely mentioning the syntax
   mid-text does nothing), and an ordinary
   reply uploads nothing. (This needs the `files:read` / `files:write` scopes, see
   [Prerequisites](#prerequisites).)
7. **See usage.** Each reply ends with a small one-line footer (context %,
   tokens, cost, duration) unless the operator disabled `SHOW_USAGE` (default
   on); fields that a backend does not report are omitted.
8. **Run background work.** Ask an agent explicitly for long-running or
   background work and it can end its reply with one of two markers. For a
   long TASK (a survey, research, a build to interpret) it prefers
   `<<spawn: ...>>`, which starts a detached subagent run of the same
   agent from a self-contained task prompt. A claude-backed agent's subagent
   INHERITS the whole conversation (it forks the thread's session; the
   original is untouched); a codex-backed agent's subagent starts fresh and
   peon prepends the thread's recent Slack transcript to the spawn prompt,
   capped, so it has the conversation to refer to even when the prompt is
   terse (a flat 1:1 DM or a scheduled run has no transcript and the task
   goes alone). For a
   verbatim shell command you
   want run as-is (a sweep, a big build, a long download) it uses
   `<<job: ...>>` naming one shell command. Either way the work is started
   detached in the thread's workdir (it keeps running even if peon restarts),
   and when it finishes the agent posts a follow-up in the same conversation
   summarizing the result (a spawn's final answer, or a job's exit code and
   output tail; the raw note is posted directly if the agent is mid-run at
   that moment). Like the files marker, only a marker at the very end of the
   reply counts, and an ordinary reply never starts one. `!job list` /
   `!job kill <id>` (see the control phrases below) show or terminate that
   agent's running jobs and spawns. Two guardrails (both configurable in
   `.env`, `0` disables either): a job or spawn is killed after
   `AGENT_TIMEOUT_MIN` minutes (the same knob that bounds a live CLI run;
   default 2880) and its result delivered with a "timed out" label, and at
   most `JOB_MAX_CONCURRENT` jobs and spawns combined (default 16, global
   across all agents) run at once; an over-limit request is declined with a
   note, never queued.

### Per-thread control phrases

Inside a thread, a message that STARTS with `!` is a command to that agent for
THIS thread only (it is acked and does not run the agent). Type it after the
mention; a command may span multiple lines (e.g. a `!cron add` whose prompt uses
Shift+Enter):

| Command | Effect (scoped to this thread + agent) |
|---------|----------------------------------------|
| `!model <model-id>` | Override the model for this thread. |
| `!effort <low\|medium\|high\|xhigh\|max>` | Override the reasoning effort. |
| `!reset` | Clear this thread's overrides (back to defaults). |
| `!new` | Start a new conversation: drop this conversation's stored CLI session so the NEXT message begins with fresh context (overrides kept). Works in any thread; especially useful in a 1:1 DM to cut the ever-growing rolling DM context. Declined while a run is in flight (`!stop` it first). |
| `!stop` (or `stop`, `interrupt`, `ctrl-c`) | Interrupt the run in flight in this thread (the Ctrl-C analog): SIGINTs the streaming CLI and settles with the partial reply, marked `_(interrupted)_`. The thread stays resumable. Streaming only; a no-op under `STREAM_OUTPUT=0`. |
| `!cron add "<min hour dom month dow>" <prompt>` | Schedule a recurring run of `<prompt>` in this thread. |
| `!cron list` | List scheduled crons. |
| `!cron remove <id>` / `!cron on <id>` / `!cron off <id>` | Delete / enable / disable a cron by id. |
| `!job list` | List THIS agent's background jobs and spawns across all conversations (id, conversation, pid, command or `spawn:` task). |
| `!job kill <id>` | Terminate one of this agent's jobs or spawns (SIGTERM to its whole process group). The result still arrives via the normal job-completion follow-up; another agent's job id reads as "no such job". |

**One run per thread.** While an agent is still working in a thread, a new
message to it in that thread is declined with a short "still working" note:
wait for the run to finish, or `!stop` it first. A scheduled `!cron` fire that
lands while a run is already in flight in its thread is skipped the same way
(its placeholder notes the skip).

**SECURITY: agents run with full unsandboxed machine access.** Every agent runs
FULLY UNSANDBOXED (claude `--permission-mode bypassPermissions`, codex
`-s danger-full-access`): any Slack-reachable agent has full read/write access to
the host machine (any path, any command) with no approval step. This is deliberate
for a personal/lab deployment; restrict who can reach the bots accordingly. Each
thread runs in its own per-thread workdir (default `~/Projects/.peon-workdirs`, set
`WORKDIR_BASE` to override) as the run's cwd, so files it produces are uploaded
back into the thread. `WORKDIR_BASE` is set in `.env` (see `.env.example`);
`SHOW_USAGE` and `STREAM_OUTPUT` are code-level toggles that default ON and are
not listed in `.env.example` (add either to `.env`, set to `0`, only to disable
it).

**What triggers a response:**

- Agents respond only when you @-mention them to start, and afterward they follow
  along inside threads they are already part of. They ignore ordinary channel
  messages that are not directed at them.
- In a 1:1 DM every message you send wakes the agent, no mention needed: the
  DM itself is one rolling conversation, and threads inside the DM behave like
  threads anywhere else.
- If several agents share a channel, an unmentioned reply inside a thread wakes
  only agents that already have a session in that thread. A follow-up that OPENS
  with an @-mention of someone else (e.g. another bot) is treated as directed at
  them and skipped (a mid-text
  mention still counts as a follow-up for the agents already in the thread).
  @-mention another agent
  to bring it into the thread; it will receive the visible thread history as
  context.
- Agents never trigger each other. Any message posted by a bot (including one that
  @-mentions another agent) is ignored, so only a human message wakes an agent. To
  hand a thread to another agent, @-mention it yourself; there is no autonomous
  bot-to-bot relay (and so no bot-to-bot loops).

## How to add a new agent (e.g. Euclid)

Four steps:

1. Append **one entry** to `agents.json`. For a claude agent (use `"backend":
   "claude"`):
   ```json
   {"name": "euclid", "display_name": "Euclid", "backend": "claude", "claude_agent": "unarylab-research:some_other_agent", "model": "claude-opus-4-8[1m]", "effort": "high"}
   ```
   (set `"claude_agent": null` for a general run, no `--agent` flag). For a
   Codex-backed agent (like Dijkstra), use `"backend": "codex"` and omit
   `claude_agent` (Codex has no subagent concept):
   ```json
   {"name": "euclid", "display_name": "Euclid", "backend": "codex", "model": "gpt-5.5", "effort": "high"}
   ```
   A codex entry may add an OPTIONAL `"codex_profile"`, naming an
   operator-installed codex persona (see the intro above). Omit it for a plain
   run:
   ```json
   {"name": "euclid", "display_name": "Euclid", "backend": "codex", "codex_profile": "euclid", "model": "gpt-5.5", "effort": "high"}
   ```
   Set the `"model"` and `"effort"` fields for this agent (every shipped entry
   does; the fallback rules for an omitted field are described in the intro).
2. Generate Euclid's Slack app manifest with `python -m src manifest euclid` (or
   `python -m src manifest euclid --write` to save it as
   `manifests/manifest-euclid.json` instead of printing; `--write` with no name
   writes all agents' manifests) and create her Slack app *From a manifest*,
   enable Socket Mode, and install it.
3. Set Euclid's two env vars: `SLACK_BOT_TOKEN_EUCLID` and `SLACK_APP_TOKEN_EUCLID`.
4. **Apply the change.** Either **hot-reload** (no restart, recommended) by sending
   `SIGHUP` to the running process: `kill -HUP <pid>` (or `systemctl --user reload
   <name>`). The process re-reads `agents.json` + `.env` and brings up just Euclid's
   connection; every already-running agent keeps its live connection untouched. Or
   **restart the process** (`python -m src`, or `systemctl --user restart <name>`);
   in-flight conversations resume from `sessions.json`, so no thread context is lost
   either way. See [Hot-reload (SIGHUP)](#hot-reload-sighup) below for the
   edit-both-files caveat.

That is all. A reload (or the next start) brings up `@Euclid` with zero changes to
`src/app.py`, `src/runners/`, or the runner modules, and Euclid gets his own
independent per-thread sessions automatically.

## Hot-reload (SIGHUP)

You do **not** have to restart to pick up changes. Send the running process
`SIGHUP` and it re-reads `agents.json` + `.env` and reconciles its live Slack
connections in place:

```sh
kill -HUP <pid>            # or, under systemd:
systemctl --user reload peon   # the unit maps reload -> SIGHUP
```

**What happens.** The process re-reads `agents.json` + `.env` and acts on only
what changed: a newly startable agent is connected, a removed agent (gone or
missing a token) is dropped, and an agent whose `agents.json` entry or either
token changed is restarted. Every agent you did **not** edit is left completely
untouched, so live conversations on those agents are never interrupted.

**Crash-safe.** If the new `agents.json` is missing or invalid JSON, or any step of
the reload fails, the reload is skipped: a warning is logged and **all running
agents are left exactly as they were.** A bad reload never drops a live agent and
never kills the process.

**Caveat: finish editing BOTH files before you reload.** One `SIGHUP` reads
`agents.json` and `.env` together, so make all your edits to both first, then send
the signal once. Reloading mid-edit (e.g. the new token not yet in `.env`) just
means that agent is treated as not-yet-startable until the next reload.

> POSIX only (macOS/Linux). For the full reconcile/diff mechanics, see
> [ARCHITECTURE.md](ARCHITECTURE.md).

## Prerequisites

1. **The `claude` CLI** (for the claude-backed agents), installed and
   authenticated, with the `unarylab-research` plugin available (so `--agent
   unarylab-research:project_manager` resolves). Verified against claude CLI
   2.1.187.
1b. **The `codex` CLI** (only if a Codex-backed agent like Dijkstra is configured),
   installed, authenticated, and on `PATH`. Verified against codex-cli 0.141.0.
   Both CLIs just need to be on `PATH`, so this works the same on Linux and
   macOS. (Runs are fully unsandboxed on both: codex `-s danger-full-access`,
   claude `--permission-mode bypassPermissions`.)
2. **One Slack app per agent (with Socket Mode enabled).** For each of Aristotle,
   Brunel, Cicero, Dijkstra, create a separate app from its manifest, which you
   generate from `agents.json` with `python -m src manifest <name>` (Slack:
   *Create New App* -> *From a manifest*, pasting the printed JSON). Run
   `python -m src manifest` with no name to print every agent's manifest as a
   JSON array at once. For EACH app:
   - **Bot scopes** (already in the manifest): `app_mentions:read`, `chat:write`,
     plus `channels:history`, `groups:history`, `im:history`, `mpim:history` so
     the bot can read
     threaded replies it should continue (including in group DMs), and
     `files:read` / `files:write` so it
     can read attachments and upload files it produces.
   - **Event subscriptions** (already in the manifest): `app_mention` (and
     `message.channels`, `message.groups`, `message.im`, `message.mpim` for
     thread follow-ups).
   - **Existing apps:** if you created an app from an older manifest (before the
     `mpim:history` scope and `message.mpim` event, or before the `files:*`
     scopes), regenerate its manifest with `python -m src manifest <name>`,
     update the app from it (or add the scope/event by hand), and REINSTALL the
     app so the additions take effect.
   - **Socket Mode**: enabled. Create an **App-Level Token** (Basic Information
     -> App-Level Tokens) with the `connections:write` scope (`xapp-...`).
   - Install the app to your workspace to get its **Bot User OAuth Token**
     (`xoxb-...`).
   - Set that app's two tokens into the env vars suffixed by the agent's
     uppercased name: `SLACK_BOT_TOKEN_ARISTOTLE` / `SLACK_APP_TOKEN_ARISTOTLE`,
     `SLACK_BOT_TOKEN_BRUNEL` / `SLACK_APP_TOKEN_BRUNEL`,
     `SLACK_BOT_TOKEN_CICERO` / `SLACK_APP_TOKEN_CICERO`,
     `SLACK_BOT_TOKEN_DIJKSTRA` / `SLACK_APP_TOKEN_DIJKSTRA` (eight vars total). Dijkstra's
     pair is optional: leave it unset and Dijkstra is simply skipped at startup.
3. **Python via conda** (env `peon`):
   ```sh
   conda run -n peon pip install -r requirements.txt
   ```

Copy `.env.example` to `.env` and fill in the tokens you have. The process loads
`.env` automatically on startup (via `python-dotenv`). An agent is started only
if BOTH of its tokens are set; agents with a missing token are skipped with a
warning, so you can run with just one configured agent.

**`.env` is authoritative: it overrides shell-exported environment variables.**
It is loaded first and with `override=True`, so a value in `.env` wins over any
matching variable already exported in your shell. This applies to every config
var, including `SESSIONS_PATH` and the `AGENT_TIMEOUT_MIN` timeout (the
session-store path is resolved live at store access, so it honors `.env` even
though it is read early at import time). A malformed `AGENT_TIMEOUT_MIN` (e.g.
`90m`) logs a warning and the default is used; it never kills the process at
startup.

**Skills that need extra environment or web access.** peon spawns the CLI with no
explicit `env=`, so every variable in `.env` is inherited by the `claude`/`codex`
subprocess (and any skill it runs). Put any value a skill expects from your shell
but that a service manager (launchd/systemd) does NOT inherit here, rather than
hardcoding it into the OS-specific `deploy/` templates: e.g. `OBSIDIAN_VAULT_PATH`
for the `obsidian-*` research skills, set to your vault root (the folder
containing `research/`). Web tools are gated by the CLI itself, not by peon, and a
headless run cannot prompt for permission, so pre-approve them once: for
Claude, add `WebSearch` / `WebFetch` to `permissions.allow` in
`~/.claude/settings.json`; for Codex, set `[tools] web_search = true` in
`~/.codex/config.toml`.

## Running always-on

`python -m src` runs in the foreground and stops when you close the terminal. To
keep the bots online continuously (surviving logout, and restarting after a crash
or reboot), run that same command under a process manager. The repo ships ready
units under `deploy/` for the two common managers; use whatever you already have.
One gotcha: service managers start with a stripped-down `PATH`, so make
sure `conda` (or your Python) and the `claude`/`codex` CLIs are reachable from the
unit.

**Quick / portable (Linux or macOS), no files:**

```sh
nohup conda run -n peon python -m src > peon.log 2>&1 &
```

**Linux (`systemd --user`):** copy the shipped unit into place, edit the two
marked paths (`WorkingDirectory` and the conda path in `ExecStart`), then enable
it:

```sh
cp deploy/peon.service ~/.config/systemd/user/
# edit ~/.config/systemd/user/peon.service: WorkingDirectory + ExecStart conda path
systemctl --user daemon-reload
systemctl --user enable --now peon
```

Manage it with `systemctl --user status|reload|restart|stop peon`
(`reload` sends SIGHUP for a hot config reload, `restart` is for code changes)
and follow logs with `journalctl --user -u peon -f`.

**macOS (`launchd`), reboot-persistent always-on:** copy the shipped LaunchAgent
to `~/Library/LaunchAgents/com.unarylab.peon.plist`, edit it for your machine,
then load it:

```sh
cp deploy/com.unarylab.peon.plist ~/Library/LaunchAgents/com.unarylab.peon.plist
# edit ~/Library/LaunchAgents/com.unarylab.peon.plist (see below)
launchctl load -w ~/Library/LaunchAgents/com.unarylab.peon.plist
```

Edit the plist for your machine:

- Set `WorkingDirectory` to your repo path (e.g. `/Users/<you>/Projects/peon`).
- Make the binaries resolvable: launchd runs with a minimal `PATH`
  (`/usr/bin:/bin:/usr/sbin:/sbin`), so the template's `/usr/bin/env conda` and
  the bare `claude`/`codex` the runners spawn both need an
  `EnvironmentVariables` > `PATH` key covering your conda dir and `~/.local/bin`
  (or replace `env conda` with conda's absolute path).
- The rest is already set in the template: the command is wrapped in
  `caffeinate -i` (holds off idle sleep during long runs), `RunAtLoad` and
  `KeepAlive` are `true` (start at login/boot, auto-restart on crash, which is
  what makes it survive reboots), and `ThrottleInterval` is 30s (no relaunch
  hot-loop after a crash).
- To log to a file, add `StandardOutPath` / `StandardErrorPath` keys (e.g.
  `peon.log`). Runtime config belongs in `.env`, not the plist (see the
  template's comments).

Manage it:

- **Status:** `launchctl list | grep peon` (a `Status` of `0` means healthy).
- **Hot config reload (no restart):** after editing `agents.json`/`.env`, send the
  running process `SIGHUP` with `kill -HUP <pid>` (the process logs its current HUP
  PID on startup; see [Hot-reload (SIGHUP)](#hot-reload-sighup)).
- **Full restart (for code changes):**
  `launchctl kickstart -k gui/$(id -u)/com.unarylab.peon`.
- **Stop / disable:** `launchctl unload -w ~/Library/LaunchAgents/com.unarylab.peon.plist`.
- **Logs:** wherever you pointed `StandardOutPath` (e.g. `peon.log`).

This is the macOS equivalent of the `systemd` unit above; `deploy/peon.service` is
the Linux always-on option and `deploy/com.unarylab.peon.plist` is the macOS one.

## Self-check

Run the test suite to confirm the bot's wiring is intact (registry loading, runner
argv, session handling) before deploying or after editing config or code. The
tests run offline and mocked: no Slack connection and no real `claude`/`codex`
calls.

```sh
conda run -n peon python -m pytest tests/ -q
```

"""peon: always-on multi-agent Slack bots, one Slack app per agent.

Importing this package has NO side effects and needs NO tokens or network.
Module layout:
  - agents.py   the registry (single source of truth) + token helpers
  - runners/    the backend runners (claude/codex) behind the unified answer() seam
  - store/      vendor-neutral persistence (sessions, overrides, crons, jobs, workdir)
  - slack/      the Slack layer (Bolt app build, handlers, control phrases, cron, jobs)
  - app.py      thin facade re-exporting the slack/ surface (the test patch targets)
  - env.py      authoritative .env loading;  manifest.py: Slack-app manifests

agents, runners, and store stay importable without slack-bolt installed; only
the Slack layer (src/slack/ and the src/app.py facade) imports slack_bolt.

Intra-package imports are relative (e.g. `from . import agents`), so the package
directory name ("src") is not hardcoded in the code and a future rename is trivial.
"""

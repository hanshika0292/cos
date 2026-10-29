# cos: a chief of staff for parallel coding agents

If you run several Claude Code, Gemini CLI or Codex sessions at once (across terminal tabs, accounts and
remote VMs, alongside browser tabs and docs), **cos** keeps track of them so you don't have to.

- **Menu bar view** of every agent session: ⏳ needs you · ⚙️ working · ✅ your turn · 💤 idle.
  Click one to jump straight to its terminal tab, tmux pane or VM.
- **Notifications** when a session is blocked on a permission prompt, or when a long task finishes.
- **Today / tomorrow to-dos**, from the menu bar or `cos add "…"`.
- **Daily brief** (optional): a short Claude Haiku summary of what each work stream did, what's pending,
  and what needs you, including the docs and tabs you had open. It opens as a clean page in your browser.

It's built to be light: there's no daemon and no local model. Agents report through hooks that append one
line to a file. The menu bar reads local files once a minute (about 0.1 s of CPU), and a background
snapshot of tabs and windows runs every 5 minutes. The only LLM call is the brief.

## Install (macOS)

```sh
curl -fsSL https://raw.githubusercontent.com/hanshika0292/cos/main/install.sh | sh
```

Or clone the repo and run `./install.sh`. A short guided setup then:

1. finds your agents (`~/.claude`, `~/.claude-profiles/*`, `~/.gemini`, optionally Codex) and adds cos's
   hooks **alongside** any hooks you already have (backups saved as `settings.json.cos-bak`)
2. installs [SwiftBar](https://github.com/swiftbar/SwiftBar) for the menu bar (free, via Homebrew)
3. asks whether to track browser tabs, terminal tabs and app windows
4. offers to track any VM you're currently ssh'd into
5. offers a daily brief at 09:00 and 18:00

Requirements: macOS, Python 3.6+ (`xcode-select --install`), git. Claude Code is needed for briefs.
Run `cos doctor` at any time to check that everything is wired up.

## What's supported

| | |
|---|---|
| **Agents** | Claude Code (multiple accounts via `CLAUDE_CONFIG_DIR`), Gemini CLI, Codex (experimental, wraps its `notify` setting) |
| **Jump-to-session** | Orca, iTerm2, Terminal.app, tmux (local and on VMs); Ghostty, Warp, VS Code and WezTerm are brought to the front |
| **VMs** | anything you can reach with key-based `ssh` (user@host, ssh-config aliases, Tailscale) and `gcloud compute ssh` |
| **Window tracking** | Chrome, Safari, Arc, Brave and Edge tabs (never incognito), iTerm and Orca tabs, other apps' window titles |
| **Docs it recognises** | Google Sheets / Slides / Docs / Forms, Figma, Notion, Canva, Linear, GitHub PRs and issues, Claude artifacts |

## Everyday commands

```text
cos                         status by work stream + today's to-dos
cos add "text"              to-do for today   (-t tomorrow · -w someday · -d 2026-11-03 · "tmr: …")
cos done 3 · drop 3 · move 3 tomorrow · today · tomorrow · todos · log
cos brief [--apply]         AI brief now (--apply adds its suggested to-dos)
cos open-brief              open the latest brief in your browser
cos schedule 09:00 18:00    run the brief daily (cos schedule --off)
cos remote add NAME TARGET  track a VM: user@host, an ssh alias, or gcloud:INSTANCE:ZONE:PROJECT
cos windows                 what tabs/windows cos has seen today
cos config                  settings (see below)
cos doctor · setup · uninstall [--purge]
```

## Privacy

Everything stays in `~/.cos` on your Mac:

- **Sessions:** prompts, Claude's last reply, and folder names
- **Windows:** titles and URLs, kept for 7 days
- **To-dos and briefs**

Nothing is sent anywhere, except when a brief runs: it sends that digest to Claude through your own
`claude` CLI and account.

- Incognito windows are never recorded. Tabs matching mail, banking, password-manager and login pages are
  skipped by default; change this with `cos config snap_exclude add <text>` or `cos config snap_exclude rm <text>`.
- `cos config brief_windows false` keeps window titles out of the brief.
- `cos config snap false` turns window tracking off entirely.
- `cos config notify_done_min 10` sets the "long task finished" notification threshold (0 = off).

## How it works

```
agent hooks ──► ~/.cos/events.jsonl ─┐
Claude's live session files ─────────┼─► ~/.cos/cos.db (SQLite) ─► menu bar · cos CLI · brief
VMs: `cos.py export` over ssh ───────┤
snap.js (tabs/windows, every 5 min) ─┘
```

- `hooks/cos-hook.sh` is a POSIX shell script. It writes one JSON line per event and prints nothing.
  It skips tool payloads (only the session id is kept) and the brief's own `claude -p` runs.
- `cos.py` is a single stdlib-only Python file. The same file runs on VMs (`cos remote add` copies it
  there) and answers `cos pull` with new events plus its live sessions.
- `snap.js` is JXA (JavaScript for Automation). It makes one bulk Apple Event per app and never launches
  an app that isn't already running.

The first time window tracking runs, macOS asks whether SwiftBar may control your browsers (Automation).
Other apps' window titles also need SwiftBar enabled under Accessibility.

## Uninstall

```sh
cos uninstall          # removes hooks (also on VMs), the menu bar plugin and the schedule; keeps ~/.cos
cos uninstall --purge  # …and deletes ~/.cos
```

## Development

```sh
sh tests/smoke.sh      # hook → ingest → CLI round-trip in a throwaway COS_HOME
```

MIT licensed. Issues and PRs are welcome, especially adapters for more agents and terminals.

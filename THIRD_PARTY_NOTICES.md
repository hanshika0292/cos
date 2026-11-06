# Third-party notices

cos contains no vendored, copied or adapted third-party code, fonts, icons, images or data.
Everything in this repository is covered by the [MIT License](LICENSE), Copyright (c) 2026 Hanshika Gupta.

cos has no package dependencies. `cos.py` uses only the Python standard library, the hook is a
POSIX shell script, and `snap.js` runs on macOS's built-in JavaScript for Automation (`osascript`).

## Tools cos works with (not included)

These are installed separately on your machine, under their own licences. cos calls them or
reads their files; it does not redistribute any of their code.

| Tool | How cos uses it | Licence |
|---|---|---|
| [SwiftBar](https://github.com/swiftbar/SwiftBar) | Shows the menu bar. `cos setup` offers to install it with Homebrew (`brew install --cask swiftbar`) and writes a SwiftBar plugin script. | MIT, Copyright (c) 2020 Ameba Labs |
| [Claude Code](https://claude.com/claude-code) | cos adds hooks to its `settings.json` and runs `claude -p` for the daily brief. | Proprietary, Anthropic PBC ([terms](https://www.anthropic.com/legal/commercial-terms)) |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | cos adds hooks to `~/.gemini/settings.json`. | Apache-2.0 |
| [Codex CLI](https://github.com/openai/codex) | cos wraps the `notify` setting in `~/.codex/config.toml` (optional). | Apache-2.0 |

The images in `docs/` are screenshots of cos itself with demo data.

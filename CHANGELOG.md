# Changelog

All notable changes to Panorama are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-10-21

### Added
- **Panorama.app**, a native Mac app (macOS 13 or later) for Apple Silicon and
  Intel, as a `.dmg` on each release. It shows the board in its own window,
  starts its own server on a free port while it is open and stops it when it
  quits (also after a crash or Force Quit), opens links to other sites in
  your browser, and can start at login. Signed and notarized by Apple.
- The Settings page shows the port as chosen by the app when the server runs
  inside it.

### Changed
- The macOS binaries are now signed and notarized, so they also run when
  downloaded with a browser.

### Removed
- `make-macapp.sh`, the Chrome-window launcher for a source checkout. The Mac
  app replaces it; source installs can still use the browser's *Add to Dock*
  or *Install app*.

## [0.1.0] - 2026-10-19

First public release.

### Added
- One tab per git repository, with every worktree of that repository as a lane
  under it. Repositories are found under `PANORAMA_ROOTS` or listed one by one
  in `PANORAMA_REPOS`; worktrees are found through `git worktree list`.
- Claude Code sessions from `~/.claude/projects`, attached to the worktree they
  ran in, with a status per session: running, waiting on you, idle or stale.
- One colour scale for tabs, lanes and session dots: red (blinking) when a
  session waits on you, green when one is working or recently finished, amber
  for uncommitted changes, blue for open todos, grey for nothing.
- Notes and todos per project, per worktree and in an inbox, stored in a local
  SQLite file. Notes save as you type.
- Ticket keys such as `ABC-123` taken from branch names, optionally linked to
  Jira with `PANORAMA_JIRA_BASE`.
- A Settings page at `/settings`, opened from the gear in the header. It holds
  the light, dark or system theme, stored server-side, and every
  `panorama.env` option: shown with where its value comes from, validated, and
  saved back to `panorama.env` with comments kept. Most options apply without a
  restart.
- Requests are only answered when addressed to `127.0.0.1` or `localhost`, and
  `git` is always run with `core.fsmonitor` off.
- A web app manifest and icons, so Safari's *Add to Dock* or Chrome's *Install
  app* gives Panorama its own window. `make-macapp.sh` builds a Dock launcher
  for a source checkout.
- Single-file binaries for macOS (Apple Silicon and Intel) and Linux, attached
  to each GitHub release with a `SHA256SUMS` file. No Python or uv needed.
- `panorama.env` lookup: `$PANORAMA_ENV`, then `panorama.env` in the current
  directory, then `~/.config/panorama/panorama.env`.
- A startup summary on stderr: which `panorama.env` was loaded, which folders
  and repos are scanned, and which are missing. A `PANORAMA_ENV` that points
  at no file gets a warning.
- A launchd job for starting at login on macOS.

[Unreleased]: https://github.com/kevalo-be/panorama/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kevalo-be/panorama/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kevalo-be/panorama/releases/tag/v0.1.0

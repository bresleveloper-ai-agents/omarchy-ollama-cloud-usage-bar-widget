# DETAILS — deep notes for the next session (human or AI)

Everything non-obvious about this plugin: why it's shaped this way, what was
verified, what was tried and rejected, and the traps. Read this before
changing anything.

## 0. Handoff / current status (2026-09-28)

- **State:** v1.0.0 works and is in daily use. It's enabled in the bar right
  after `omarchy.agents` and shows the outline-robot glyph 󱚤, which the user
  approved. The UI was verified with screenshots (`preview.png`).
- **Published (public since 2026-09-28)** at https://github.com/bresleveloper-ai-agents/omarchy-ollama-cloud-usage-bar-widget, under the
  agents' GitHub account (not the user's personal one). The repo name follows
  the user's hard rule: Omarchy repos start with `omarchy-`, and long,
  descriptive names are preferred. Pushes use the token in
  `~/Projects/agents-data/github-token` through a one-off credential helper,
  so no token is stored in `.git/config`:
  ```bash
  git -c credential.helper= -c 'credential.helper=!f(){ echo username=x-access-token; echo "password=$(cat ~/Projects/agents-data/github-token)"; }; f' push
  ```
  Before it went public, all history was scanned for the Ollama key, the
  GitHub token and generic secret patterns: nothing was found.
  Marketplace submission: github.com/omacom/omarchy-plugin-marketplace
  (GePi0's issue #7629 is a template).
- **Identities:** commits are made as `ariel <arielbattlenet@gmail.com>`,
  the agents' GitHub email, set in this repo's `.git/config`. Human-facing
  contact in README and LICENSE is `ariel.rubi@gmail.com`. Don't swap them.
  History was rewritten once, on 2026-09-28 right after the first push, to
  move commits from ariel.rubi@ to arielbattlenet@.
- **Secrets:** the API key lives **outside** the repo at
  `~/.config/omarchy/ollama-usage/api.key`. `.gitignore` also blocks `*.key`,
  `.env*` and state files. Before any push, re-check with
  `git log -p | grep -F "<key fragment>"`. It was verified clean at the first
  commit. The key was once pasted into a chat session, so rotating it at
  ollama.com/settings/keys is advisable.
- **Where things are:**
  - The repo is the real plugin folder, `~/.config/omarchy/plugins/io.github.bresleveloper.ollama-cloud-usage-bar-widget/`.
  - `~/Projects/omarchy-ollama-cloud-usage-bar-widget` is a symlink to it (see §2 for why).
  - State lives in `~/.local/state/omarchy/ollama-usage/usage.json`.
- **Nothing else on the system depends on this.** The earlier systemd timer
  approach was fully removed (§6).
- **Next ideas:** see §9.

## 0.05 Marketplace readiness (checked 2026-09-28)

Checked against the marketplace's own code (clone of
omacom/omarchy-plugin-marketplace, `scripts/build-catalog.mjs`
`validateManifest` and `scripts/security-baseline-scanner.mjs`
`runSecurityBaseline`, run against the pushed commit):
- **Manifest:** valid as a community plugin.
- **Security baseline:** outcome `passed`, disposition `clear`, 0 findings
  and 0 capabilities. The scanned files were LICENSE, Panel.qml, README.md
  and collect.py.
- **Repo:** public; root README, LICENSE and preview.png present; no
  symlinks; entry point present; ID neither listed nor retired.
- **Display name:** changed to "Ollama Cloud Usage Bar Widget" because
  GePi0's listed plugin is already called "Ollama Cloud Usage".
- **Keep it passing:** avoid the words sudo, pkexec, systemctl, systemd-run
  and curl|sh in the README and code. They add review capabilities or
  findings. The scan checks each new commit before an update is published.
- **Submission:** an issue titled "[Plugin]: Ollama Cloud Usage Bar Widget"
  in omacom/omarchy-plugin-marketplace, using the SUBMISSION.md body format
  with category Widgets and tags ai, bar, quickshell.
- **Updates after listing:** use the "Plugin verification" form with
  "Verify and publish a newer upstream commit" and the full 40-character SHA.

## 0.1 Plugin ID rename (2026-09-28)

The ID changed from `ariel.ollama-usage` to
`io.github.bresleveloper.ollama-cloud-usage-bar-widget` before the marketplace
submission.
- **Why:** marketplace IDs are permanent, and the `ariel.*` prefix was
  already used by an unrelated author (gustavohariel's `ariel.singbox`,
  `ariel.hubstaff`). The marketplace guide recommends `io.github.<name>.<plugin>`.
- **Who "bresleveloper" is:** the user's own GitHub name. The repo itself
  lives under the agents' account `bresleveloper-ai-agents`.
- **What changed:** the folder name, `manifest.id`, `moduleName` /
  `ipcTarget` in Panel.qml, and the README commands. The key path and state
  path (`.../ollama-usage/`) did **not** change; they are independent of the ID.

## 1. What this is

`io.github.bresleveloper.ollama-cloud-usage-bar-widget` is a third-party Omarchy shell (Quickshell/QML) bar
widget. It shows Ollama Cloud plan usage: limit windows (session, weekly, and
monthly if the API ever returns it) and per-model request counts per window.
Built 2026-09-28 on Omarchy 4.0.3, Ollama CLI 0.33.3, Arch Linux.

Two files do the work:

- **`collect.py`** (Python 3, stdlib only). Calls the API, writes one JSON
  state file atomically, and always exits 0; errors go inside the record.
- **`Panel.qml`**. The bar button and popup. It runs `collect.py` on timers,
  watches the state file, and draws it. It never talks to the network itself.

## 2. Location and install layout (important)

- **The real folder is `~/.config/omarchy/plugins/io.github.bresleveloper.ollama-cloud-usage-bar-widget/`, which
  holds the git repo.**
- `~/Projects/omarchy-ollama-cloud-usage-bar-widget` is a **symlink to it**. It is not the
  other way round.
- Why: the shell hot-reloads plugins with `inotifywait -m -r` on
  `~/.config/omarchy/plugins` (`/usr/share/omarchy/shell/services/PluginRegistry.qml` ~663-679).
  inotify does not follow symlinks, so edits in a symlinked plugin folder never
  trigger a reload. Discovery itself works through symlinks (the scan uses
  `"$dir"/*/`). With the real folder here, saving any file reloads the plugin.
  If a reload somehow doesn't happen: `omarchy-shell shell rescanPlugins`.
  **Observed:** changing a string literal (the default bar glyph) in
  Panel.qml/manifest.json hot-reloaded without error, but the bar kept the old
  glyph even after `rescanPlugins`. Only `omarchy restart shell` picked it
  up. When a change doesn't show, restart the shell before debugging.
- `omarchy plugin validate` rejects symlinks inside a plugin folder
  (`find ... -type l`), so every asset must be a real file. Validate the
  real path: `omarchy plugin validate ~/.config/omarchy/plugins/io.github.bresleveloper.ollama-cloud-usage-bar-widget`.
- Never edit `/usr/share/omarchy/` (it belongs to the omarchy package). Read
  it freely.

## 3. Data source: `GET https://ollama.com/api/usage`

- Auth: `Authorization: Bearer <API key>`. Keys come from
  ollama.com/settings/keys. `ollama signin` (ed25519 key in `~/.ollama`)
  does **not** work for this endpoint.
- **Undocumented.** It was found through GitHub issues (ollama/ollama#15132,
  #16448, can1357/oh-my-pi#11739). It could change or vanish; `collect.py`
  then reports "no usage windows in response; the API may have changed".
- Verified response (2026-09-28, pro plan):
  ```json
  {
    "activity": {"cost": "0.00000",
                 "period": {"type": "last_4_weeks", "starting_at": "...", "ending_at": "..."},
                 "models": []},
    "limits": {
      "session": {"usage": 0.066, "models": [{"name": "kimi-k2.7-code", "request_count": 27},
                                              {"name": "glm-5.3-flash", "request_count": 19},
                                              {"name": "web search", "request_count": 7}]},
      "weekly":  {"usage": 0.029, "models": [...]}
    }
  }
  ```
- `usage` is a **0–1 fraction**. `collect.py` also accepts percent-scaled
  values (> 1.5 is divided by 100) in case the API changes.
- **No reset timestamps.** `activity.period` is a reporting period, not a
  reset time. Reset times come from ollama.com/settings instead (§3b).
- Plans created after 2026-08-31 reportedly have no `session` window. Some
  reports mention `monthly`. Both are handled: any window the API omits is
  simply skipped.
- A bad key returns HTTP 401. Python's default urllib user agent is accepted.
  We send `User-Agent: io.github.bresleveloper.ollama-cloud-usage-bar-widget` anyway.
- "web search" (and presumably "web fetch") appear as entries in the model
  lists. `collect.py` marks them `"tool": true`. The panel lists them after
  the models, dims them, and leaves them out of the bar scaling.

## 4. `collect.py` contract

State file: `$XDG_STATE_HOME/omarchy/ollama-usage/usage.json` (default
`~/.local/state/...`), mode 0600, written through a temp file plus `os.replace`.

```json
{
  "schemaVersion": 1,
  "checkedAt": "ISO-8601 UTC, last attempt",
  "updatedAt": "ISO-8601 UTC, last success ('' if never)",
  "ok": true,          // windows are usable (possibly stale)
  "stale": false,      // true = last attempt failed transiently, windows are from updatedAt
  "error": "",         // short fixed message, never response bodies
  "authHelp": "",      // set when the key is missing or rejected
  "windows": [
    {"id": "session", "title": "Session", "percent": 0.066, "requests": 53,
     "models": [{"name": "kimi-k2.7-code", "requests": 27, "tool": false}, ...]}
  ]
}
```

Behavior:

- **Key lookup:** `$OLLAMA_API_KEY`, then `~/.config/omarchy/ollama-usage/api.key`
  (overridable with `OLLAMA_USAGE_KEY_FILE`). It warns on stderr if the file
  is group- or world-readable. The key never goes into argv, the state file
  or logs.
- **Missing key, or 401/403:** `ok:false`, windows cleared, `authHelp` set.
- **Any other failure** (network, 429, 5xx, bad JSON, empty windows): keep the
  previous windows, set `stale:true` and `error`, keep the old `updatedAt`.
- **Concurrency:** one bar widget instance runs per monitor, each with its own
  timers. Runs are serialized with `fcntl.flock` on `<state dir>/.lock`, and a
  run is skipped if `checkedAt` is under 60 s old, unless `--force` is given.
- **Safe file opens** (marketplace review of #9116, HANCORE-linux): the lock
  is opened with `os.open(O_RDWR|O_CREAT|O_NOFOLLOW|O_NONBLOCK)`, never
  `open(..., "w")`, and must be a regular file (`fstat`/`S_ISREG`) before
  `flock`. A symlinked `.lock` can't truncate its target, and a FIFO can't hang
  the widget; either one makes the run exit 1 with a message. `usage.json` is
  read the same way (no symlinks, regular files only). The key file may be a
  symlink but must resolve to a regular file. All of this is in
  `open_regular()`/`read_regular()` in `collect.py`.
- **Test overrides:** `OLLAMA_USAGE_STATE` changes the output file.
  `OLLAMA_USAGE_ENDPOINT` is honored **only** with `OLLAMA_USAGE_TEST=1`, so a
  stray inherited variable can't send the Bearer key to another host.
- **Response handling:** capped at 1 MiB, HTTP timeout 15 s. QML also wraps
  the run in `timeout -k 5 30`.

Test recipe (all verified on 2026-09-28):

```bash
cd ~/.config/omarchy/plugins/io.github.bresleveloper.ollama-cloud-usage-bar-widget
S=/tmp/ou-test.json
OLLAMA_USAGE_STATE=$S ./collect.py --force && jq . $S                        # happy path
OLLAMA_API_KEY=bad OLLAMA_USAGE_STATE=$S ./collect.py --force; jq '{ok,error}' $S   # 401 path
OLLAMA_USAGE_STATE=$S ./collect.py --force   # restore good data first, then:
OLLAMA_USAGE_TEST=1 OLLAMA_USAGE_ENDPOINT=https://nonexistent.invalid \
  OLLAMA_USAGE_STATE=$S ./collect.py --force; jq '{ok,stale,error}' $S        # stale path
OLLAMA_USAGE_ENDPOINT=https://nonexistent.invalid OLLAMA_USAGE_STATE=$S ./collect.py --force  # ignored without TEST=1
```

## 5. `Panel.qml` notes

- **Base type** is `qs.Ui` `Panel` (`/usr/share/omarchy/shell/Ui/Panel.qml`).
  It gives `bar`, `settings`, `opened`, `open/close/toggle`, `switchPanel`,
  and `setting(name, fallback)`.
- **`manageIpc: false` plus our own `IpcHandler`.** The base Panel already
  registers a handler on `ipcTarget`. Adding a second one for `refresh` would
  collide, so we turn the base one off and re-declare
  open/close/show/hide/toggle/refresh. The stock agents widget does the same.
- **Manifest defaults are NOT merged** into the bar entry: enabling writes
  only `{id}` into shell.json. Every setting is therefore read as
  `setting("key", fallback)`, and the interval is clamped to at least 60 s.
  `showPercent` is an enum `"Off"|"On"`: shipped manifests use no boolean
  schema type.
- **Bar button:**
  - `BarIconButton` forces `labelVisible: false` and a fixed width, so it
    can't show text. With `showPercent` On, a plain `WidgetButton` shows
    "icon NN%" instead.
  - Both buttons exist, and `visible` picks one. `KeyboardPanel.anchorItem`
    follows the visible one.
- **`LimitRow`, `Meter` and `ModelRow` are copied** from
  `/usr/share/omarchy/shell/plugins/agents/Panel.qml` (inline components
  there, not exported by `qs.Ui`). Keep them in sync visually if the stock
  widget changes its look.
- **Asset and collector paths** come from `Qt.resolvedUrl(...)`. The collector
  path has `file://` stripped and is `decodeURIComponent`-ed. The `Process`
  command is an argv array with no shell, so there is no injection surface.
- **FileView** watches the state file. `onExited` of the process also calls
  `reload()`, because the file may not exist when the watcher starts.
- **Timers:**
  - a 5 s one-shot after load (lets the session and network settle at login)
  - the interval timer
  - one quick retry 60 s after a stale result
  - a 30 s `nowMs` tick while the panel is open, for "updated Xm ago"
- **Opening the panel** calls `refresh(false)`; the collector's 60 s throttle
  keeps that cheap.
- **Hero icon:** tries `assets/ollama-light.svg` first on light surfaces,
  otherwise `assets/ollama.svg`, then falls back to the bar glyph. This is the
  same luminance trick as stock.
- **Bar glyph:** 󱚤 (U+F16A4, outline robot). It sits next to the stock
  Agents widget's solid robot (U+F16A3), so the pair reads as "agents"
  without being identical. The first version used 󰙴 (U+F0674, sparkles);
  the user asked for something more agent-like. Other candidates:
  U+F0D70 face-agent, U+F16A2/U+F16A6 other outline robots. Glyphs were
  previewed with
  `pango-view --font="JetBrainsMono Nerd Font 24" -o out.png text.txt`.

## 6. Relationship to other things on this machine

- The stock `omarchy.agents` widget reads any `*.json` in
  `~/.local/state/omarchy/agents/usage/`. An earlier iteration wrote an
  `ollama.json` there from a systemd user timer
  (`agent-usage-ollama.{service,timer}` and `~/.local/bin/agent-usage-ollama`).
  **All of that was removed.** This plugin deliberately writes to its own
  state dir so the Agents widget has no Ollama tab and nothing is duplicated.
  If an `ollama.json` ever reappears there, something else wrote it.
- The API key was moved from `~/.config/omarchy/agents/ollama.key` to
  `~/.config/omarchy/ollama-usage/api.key`.
- The Claude Code tab in Agents still counts Ollama cloud models used through
  Claude Code (e.g. `glm-5.3-flash`), because the stock Claude collector
  counts every model in `~/.claude/projects`. Not fixable without cloning
  that collector.

## 3b. Reset times: `resets.py` (added 2026-09-29)

- Source: ollama.com/settings, rendered with headless Chromium from a temp
  profile. The profile holds only the ollama.com cookie rows plus Local
  State's `os_crypt`, because Chrome decrypts cookies with the keyring. The
  code is adapted from GePi0's collector. It took about 1 s here with Chrome
  Default.
- **The page has exact times.** Each usage block ends with
  `<div class="... local-time" data-time="2026-09-29T15:00:00Z">Resets in 2 hours.</div>`.
  GePi0 parses only the coarse text; we read `data-time` and fall back to
  the text. Observed: session resets fall on the hour (15:00Z), weekly on
  Monday 00:00Z.
- Trap: each heading also appears in the bar's `aria-label="Session usage 13% used"`,
  so a section runs until the next *different* heading.
- Scheduling (`resets_due` in collect.py). The user asked for this not to
  be a bottleneck or an annoyance:
  - It runs only after usage.json is written, under its own non-blocking
    lock, and the whole step is wrapped in `except Exception`.
  - It refreshes every 2 h. It can run sooner, at most every 10 min, when
    a cached reset has passed (the window rolled over, and knowing this
    reset says nothing about the next one), or when a window shown on the
    page with no reset now has usage > 0 (a new session started).
  - After a failure it waits 30 min.
  - A reset that has passed is dropped from the panel right away, not
    shown as "now".
- Time caps: 25 s for all render attempts; the panel's `timeout` was raised
  from 30 s to 60 s. SIGTERM becomes `sys.exit`, so `kill_group` still reaps
  Chrome's process group. Tested with a fake hanging browser: no orphans.
- `sys.dont_write_bytecode` is set before `import resets`, because a
  `__pycache__` write in the plugin folder triggers a hot-reload.
- Setting `resetTimes` Off passes `--no-resets`.

## 7. Alternatives evaluated

- **Feeding request counts into the Agents widget's token fields.** Rejected:
  the headings say "tokens", the hover shows a meaningless in/out split, and
  "tokens by day" can't be split by model.
- **Cloning `omarchy.agents`.** Possible, but you then own a fork of about
  1,700 lines and lose upstream updates.
- **GePi0/omarchy-ai-usage** (marketplace plugin `ollama-cloud.ai-usage`,
  v1.3.0). It was installed briefly to look at, then removed. It gets plan
  tier, balance and **reset countdowns** by rendering `ollama.com/settings`
  in headless Chrome with a copy of your ollama.com cookies, then parsing
  text such as "Resets in 4 hours". Worth knowing:
  - It worked here in about 1.4 s using Chrome's Default profile.
  - It replaces `omarchy.agents` in the bar.
  - It's fragile to page markup changes and runs a browser in the background.
  - Ideas borrowed: the cold-start delay, keeping the last good data on
    failure, the Ollama mark asset.
  - Not borrowed at first: scraping. Later borrowed, only for reset times
    (§3b).
- **Estimating resets from observed usage drops.** Rejected: it would be
  wrong after idle periods, and a guessed countdown looks authoritative.

The original plan lived in PLAN.md. It was folded into this file and deleted
in the first commit; its one wrong assumption was the symlink direction.

## 8. Review log

The plan was audited on 2026-09-28 by a separate reviewer agent before
implementation. Every blocker and should-fix it raised was applied:

- symlink direction
- the `entryPoints.barWidget` key
- `manageIpc: false`
- copying the inline components
- `WidgetButton` for the percent display
- setting fallbacks
- enum instead of boolean
- validating the real path
- clear `ok`/`stale` state fields
- key-file permission warning
- endpoint override gated behind a test flag
- argv-array Process
- `reload()` on exit
- flock plus throttle for multiple monitors
- tools separated from models
- migration order: stop the timer before deleting `ollama.json`

## 9. Ideas / TODO

- Show `activity.cost` if it ever becomes non-zero (pay-as-you-go).
- A history file (append the weekly model counts each run) could later give a
  real "requests by day" chart.
- If ollama.com adds reset timestamps to `/api/usage`, use them and drop the
  scrape (§3b).
- A screenshot for the README lives in `preview.png`; update it when the UI
  changes.

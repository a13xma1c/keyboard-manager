# Keyboard Manager

A desktop application for building custom keyboard shortcuts, macros and automated
sequences. Built with Python, PySide6 (GUI), pynput (input injection, recording and
global hotkeys), the `keyboard` package (alternative hotkey backend, default on
Windows) and SQLite (storage).

## Features

| Feature | Where |
| --- | --- |
| Custom keyboard shortcuts | Give any macro a **Hotkey**: one combination (`ctrl+alt+k`) or a sequence of up to 3 chords pressed in turn (`ctrl+k ctrl+s`, 2 s between chords). *Capture* records it chord by chord |
| Record keyboard and mouse | Editor > **Record...**: choose keyboard / clicks / movement; F9 stops, F8 pauses and resumes (the paused time is left out). Input sent by Keyboard Manager itself is never recorded. Afterwards a **review** shows the steps: untick what you do not want, shorten or remove pauses, drop mouse movement, join typed lines into one text |
| Visual macro editor | Step tree with drag and drop, nesting, per-step forms, **undo / redo** (Ctrl+Z / Ctrl+Shift+Z) of every step change, and **several steps at once** (Ctrl/Shift+click: delete, duplicate, move, copy / cut / paste, also between macros) |
| Folders, tags, favorites | Nested **folders** (New folder..., drag macros onto a folder, right-click a folder to rename or delete it: its macros move up, nothing is deleted), **tags** (editor field; the filter above the list and `#tag` in the search), **★ favorites** (listed first, and run from the tray icon's *Run favorite* menu) |
| Pre-flight check | The **Check** tab lists what would go wrong before anything runs (broken steps, a missing macro to run, clashing hotkeys, an event without its filter) and what is probably unintended (a `{variable}` that is never set, a key this system does not have, a Repeat until stopped without a pause, a question in a macro that runs by itself); double-click jumps to the step. The editor shows the count next to **Save**; a macro with broken steps is not started |
| Playback speed | Per macro (**Speed** in the editor) and for all macros (**Speed** in the toolbar, multiplied together); speeds up or slows down delays and typing, also while a macro runs. Window waits and timeouts stay in real time |
| Several macros at once | Ctrl/Shift+click in the macro list, then right-click: enable, disable, export or delete them together |
| Conditions and loops | **If / else** and **While** (active app, window title, time range, or a variable compared with ==, <, contains, starts / ends with, matches a regular expression, is empty), **Repeat**, **Break**, **Delay** (+ random jitter), **Wait for window**, **Run another macro**, **Stop**, **Comment** |
| Variables | **Set variable** (set, add, subtract, append, or calculate `{n} * 2`), **Ask for input**; `{name}` in any text inserts a variable or a built-in (`{date:%d.%m.%Y}`, `{time}`, `{clipboard}`, `{app}`, `{window_title}`, `{macro}`, `{random:1-6}`). *Saved* variables keep their value between runs and are shared by all macros (**Variables** tab) |
| Clipboard, apps and messages | **Paste text** (via the clipboard: fast, any characters; the clipboard is restored), **Set clipboard**, **Open app, file or URL**, **Show notification** |
| Application-specific shortcuts | *Application* / *Window title* filters (**Detect** fills them from the window you switch to). A specific macro beats a global one on the same hotkey |
| Import / export | JSON files via **Import...** / **Export...** (selected macro or all). Imports are validated; name clashes are renamed, clashing hotkeys are cleared |
| Global hotkeys | Active while the app runs (it keeps running in the system tray when the window is closed). Toggle in the toolbar. Schedules likewise only run while the app runs |
| Execution history | **History** tab: time, macro, trigger, status (incl. `skipped` runs), duration, steps run, message. Select a run to see **each step**: when it ran, how long it took, its result and details (whether a condition held, the value set, rounds of a loop). Typed text and answers are not logged; at most 2000 steps per run; **Log each step** switches it off |
| Per-macro limits | Editor > **Limits**: stop a run after a time (paused time and a test run's countdown excluded) or a number of steps; at most once every N seconds (**cooldown**) and at most N runs **per day**. The last two apply to hotkeys, abbreviations, schedules and events; a run they refuse is listed as `skipped` with the reason. Running a macro by hand is never limited |
| Clashes with system shortcuts | Saving a hotkey that macOS, Windows or the Linux desktop uses (Cmd+Tab, Win+L, Alt+Tab...) or that is standard in applications (Copy, Save...) asks first and is flagged in the Check tab |
| Scheduled runs | Editor > **Schedule**: *Once* (date and time), *Days of the week* at a time (daily, weekdays, weekly...), or *Repeatedly* every N minutes |
| Text expansion | **New snippet...**: an abbreviation (e.g. `;sig`) and the text that replaces it. Any macro can have an **Abbreviation**: typing it deletes what was typed and runs the macro |
| Key remapping | **Remapping** tab: make a key act as another (Caps Lock as Esc, F13 as Cmd...) system-wide while the app runs |
| Event triggers | Editor > **Run on event**: when Keyboard Manager starts, or when the macro's application (its *Application* / *Window title* filters) gains or loses focus. **Auto-run** in the toolbar switches schedules and events off together |
| Pause / resume | **Pause** / **Resume** in the toolbar or tray menu, or the **pause hotkey** (default `ctrl+alt+f11`, changeable via *Pause hotkey...*, empty = none) |
| Queued runs | Editor > **When busy**: a macro started by its hotkey, abbreviation, schedule, event or a trigger while another one runs is *skipped* (default) or *queued*. Starting one by hand while busy asks whether to queue it. The **Run** tab lists the queue (remove single runs or clear it); the stop hotkey empties it too |
| Trigger engine | **Triggers** tab: rules that run a macro when another macro *finished* (optionally only on success / error / stop), the *clipboard changed* (optionally: to something containing a text), a *file* was created / changed / deleted, or an *expression became true* (`counter >= 10`, `"Invoice" in window_title`). Each rule has an optional condition (`time >= "09:00" and time < "17:00"`), can queue its run, and shows what it did last |
| Expressions and scripts | A small, safe language with Python syntax: **If / While** on an *expression*, **Set variable** with mode *expression*, `{= expression}` inside any text, and the **Run script** step (assignments, `if` / `elif` / `else`, `while`, `for`, `break`, and commands such as `type()`, `press()`, `wait()`, `click()`, `run()`, `ask()`). The **Variables** tab lists the functions and has an expression tester |
| Debugger | Editor > **Debug**: runs the macro as shown, stopping at the first step (option) and at **breakpoints** (*Breakpoint* button or F9, marked ●). **Step** runs one step, **Continue** runs to the next breakpoint. Input is **simulated** by default (nothing is typed or clicked; the Run tab shows what would have been sent); untick *Simulate input* to debug with real input after the 3 s countdown |
| Live execution view | **Run** tab: the running macro's steps with the current one highlighted, what started it, running time, steps run, the last steps with their results, the run's variables, and the queue; Pause / Continue / Step / Stop. After the run it keeps showing how it ended |
| Vision (seeing the screen) | **Wait for image** (until it appears or disappears), **Click image** (finds a template on the screen and clicks its middle, with offsets), the If / While condition *image is on the screen*, and the script commands `find("button.png")` and `pixel(x, y)`. Templates are snipped straight from the screen (**Capture...** in the step's form, 3 s countdown); matches set `found_x` / `found_y` |
| UI automation | **Focus window** brings another application's window to the front (waits until it has focus), plus the existing **Wait for window**, **Open app, file or URL** and mouse/keyboard steps; `focus("app", "title")` in scripts |
| Profiles | Named sets of macros (Work, Gaming...) switched in the toolbar, by the **Switch profile** step, or by a context rule. Macros outside the active profile are completely inactive (hotkeys, abbreviations, schedules, events, triggers); the list shows them greyed as *(other profile)*. **All profiles** (the default) has everything active |
| Keymap layers | A macro can sit on a **Layer** (editor field): its hotkey / abbreviation only works while that layer is active, and on the same hotkey the layer's macro wins over the base one. Layers are switched by the **Switch layer** step (also *one shot*: back to base after the next hotkey), the Profiles tab, or a context rule |
| Context-aware keymaps | **Context rules** (Profiles tab): *while a window matching these filters has focus, use this profile and/or layer*; when it loses focus, the previous one comes back. First matching rule wins; switching by hand always takes over |
| Sync between computers | **Sync & Remote > Sync**: point every computer at the same folder (Dropbox, iCloud Drive, OneDrive, Syncthing, a network drive). Macros are merged one by one in both directions, deletions travel too, and a macro changed on two computers is kept twice (*... (conflict from laptop)*). Optionally also profiles, context rules and trigger rules. Syncs every N minutes, shortly after you change something, and on **Sync now** |
| Team libraries | **Sync & Remote > Teams**: a shared folder as a team library. Its macros arrive as read-only *team macros* (folder *Team/&lt;name&gt;*) that follow the library; right-click > **Publish to team** shares your own; **Edit locally** makes a team macro yours so you can change and republish it. Publishing over a colleague's newer version is refused unless you confirm |
| Sharing | **Copy share code** (right-click, or the Sharing page): selected macros as one line of text (`KMSHARE1:...`) for a chat, mail or ticket; paste it into *Sharing > Import*. Imports never overwrite anything |
| Remote control | **Sync & Remote > Remote control**: a token-protected HTTP/JSON API and a **web panel** (run, stop, pause macros, switch profile / layer, sync) for a phone, tablet, Stream Deck or scripts; `keyboardmanager ctl run "Name"` from a terminal. Local only by default; the network is opt-in |
| Liquid Glass look | Translucent, layered glass panels floating over a softly coloured backdrop, a floating toolbar capsule, a segmented tab bar with a tinted "lens" for the selected tab, capsule buttons, switches instead of check boxes, rounded menus and lists. Light and dark (follows the system, or fixed), eight accent colours or the system's accent; **Appearance** in the toolbar, where **Classic** brings back the platform's native controls |
| Background service | **Start at login** (window hidden, tray icon only: LaunchAgent / Run key / XDG autostart), `--background`, and `--headless` to run without any window. Only one instance runs per user and database: starting it again brings the running one to the front |

Safety: only one macro runs at a time; the **stop hotkey** (default `ctrl+alt+f12`,
changeable in the toolbar) or pressing a running macro's own hotkey again stops it.
Held keys and mouse buttons are always released when a macro ends. **Test run**
starts after a 3 second countdown so you can focus the target window.

Pausing takes effect at the next step, typed character (with a typing interval) or
immediately inside a delay, whose remaining time is kept (the start countdown is
paused the same way). While paused, keys and mouse buttons held by the macro are
released so your own typing is unaffected, and pressed again on resume. The stop
hotkey also stops a paused macro. If a macro already uses the pause hotkey, the
macro keeps it and the pause hotkey is reported as not registered in the status bar.

Abbreviations are matched exactly (case-sensitive) as soon as the last character is
typed, so start them with a rarely typed character (`;`, `/`). What was typed is
forgotten on anything that may move the cursor (arrows, Enter, Space, clicks,
Ctrl / Cmd shortcuts); Backspace corrects. They follow the macro's application
filters and, like hotkeys, the **Global hotkeys** switch. One abbreviation may not be
how another starts (`;s` next to `;sig`) in the same scope.

Key remapping is system-level and platform-specific: **macOS** uses `hidutil` (no
permission needed; the previous mapping is restored on quit, and on the next start
after a crash), **Windows** the `keyboard` package, **Linux** the `keyboard` package
as root only (otherwise the tab explains it is unavailable). A key that is remapped
cannot also be a target (no chains or swaps). Remaps are settings, not part of
macro exports.

Placeholders are only replaced for *known* names, so texts that contain braces for
other reasons (code, JSON) are typed unchanged; `{{` and `}}` give literal braces.
Variables are looked up in the current run first, then among saved variables, then
the built-ins. The calculator only does arithmetic (`+ - * / // % **`, parentheses,
`abs round min max floor ceil`). Undo / redo covers the steps; text fields keep their
own Ctrl+Z while you type in them.

A hotkey may not be another one's beginning (`ctrl+k` next to `ctrl+k ctrl+s`) in
the same scope, and the stop / pause hotkeys may not appear in sequences. Hotkeys
are observed, not intercepted: the keys also reach the focused application (for
sequences, prefer chords the application does not use itself). Focus
events are detected by watching the focused window (only while event macros exist);
like hotkeys, events and schedules do nothing while another macro is running,
unless the macro's *When busy* is set to queue.

Schedules use local time and are never caught up: runs missed while the app was
closed are dropped. A run that cannot start within 60 seconds of its time (another
macro still running, or the computer was asleep) is skipped and listed in History as
`skipped` with the reason; many missed *Repeatedly* runs count as one. *Repeatedly*
counts from when the schedule becomes active (app start, save, or Schedules switched
on). Disabled macros do not run, and *Application* / *Window title* filters apply as
for hotkeys: if another window has focus at that moment, the run is passed over
silently. After an import, the summary says how many macros will run on a schedule.

Only one macro runs at a time; the queue holds at most 20 runs (more are listed in
History as `skipped`). Queued runs start in order as soon as the running one ends;
cooldowns and daily limits are checked when a run is queued. Stopping (stop hotkey,
toolbar) also empties the queue, so nothing queued starts typing after an emergency
stop.

Trigger rules are on while **Auto-run** is on. The clipboard, files and *becomes
true* expressions are checked once a second (only while such rules exist); the first
check after start-up or a change to the rules is the baseline, so what is already
there does not fire. *Macro finished* rules may start each other, but at most 10 runs
in a row are started that way. A rule names its macro; renaming the macro updates
the rules, and the Check tab reports rules whose macro is missing or disabled. Debug
runs never fire triggers. Rules are settings: they are not part of macro exports.

Expressions see the run's variables, the saved ones and the built-ins (`clipboard`,
`app`, `window_title`, `date`, `time`...). Variables are stored as text; reading one
gives a number for plain numbers (`"42"`, `"-1.5"`, not `"007"`) and true / false for
`"true"` / `"false"`, so `count + 1` and `price * 1.19` work; clock times compare as
text (`time >= "09:00"`). Functions: `len upper lower trim replace contains
startswith endswith substr split join lines matches number text round abs min max
floor ceil random range empty`. The language has no imports, attribute access,
function definitions or file access; texts, lists and numbers are size-limited, and
every statement and loop round of a script is a pause / stop point that counts
towards the macro's step limit. Reading a variable that does not exist is an error
(the Check tab warns about it). A script is one step for the debugger and the step
log; its `log("...")` lines appear in the step's details.

Image matching compares raw pixels of the **primary screen**: exact by default
(fast), or with a per-channel *tolerance* for anti-aliasing and colour-profile
differences (noticeably slower on big screens). Capture templates on the machine
and screen where they will be matched, keep them small and distinctive, and
re-capture after theme or scaling changes. On high-resolution (Retina) screens the
capture has more pixels than the mouse grid; found positions are converted
automatically. The screen is checked about 3 times per second while a vision step
waits. In a simulated debug run the real screen is still read, but clicks and
focus changes are only recorded.

**Focus window** is best effort per platform: macOS activates the application as a
whole (AppKit), Windows finds a visible window by title / process name and brings it
to the foreground, Linux asks `xdotool` (X11). The step watches the focused window
and keeps asking until it matches or the timeout passes.

A macro can be restricted to **profiles** (editor field, comma separated; empty =
all). The active profile is chosen in the toolbar and remembered; switching to an
unknown name (e.g. from a script) creates it. Renaming a profile updates the macros,
context rules and the current choice; deleting one only lifts the restriction, no
macro is deleted. Manual runs and the editor ignore profiles, so you can always test
a macro.

**Layers** only gate hotkeys and abbreviations - schedules, events and trigger rules
run regardless of the layer (use profiles to silence those). The layer resets to
base when the application starts. A typical setup: one macro per layer key
(`ctrl+alt+n` > *Switch layer "nav"*, one shot), and the layer's macros on
single-chord hotkeys. The status bar shows the active layer.

Context rules are checked top to bottom a few times per second (only while rules
exist and the checkbox in the Profiles tab is on); the first rule matching the
focused window wins. A rule can switch the profile, the layer, or both; "" means
*All profiles* / the base layer, while an unticked part is left alone. When no rule
matches any more, the profile you chose by hand and the base layer come back.
Context rules are settings (like key remaps), not part of macro exports.

**Sync** keeps one file per macro in `<folder>/keyboardmanager-sync/`, written
atomically. For every macro it compares this computer's version, the folder's and
the version both agreed on at the last sync, so it knows who changed what; a file
that is merely missing (a sync client still downloading) never deletes anything, an
unreadable one is skipped until the next round, and a missing or unplugged folder is
reported instead of being treated as empty. When a macro arriving from another
computer clashes with one here by name or shortcut, every computer settles it the
same way (one of them gets *(2)* or loses the shortcut), so clashes do not bounce
back and forth. Settings (profiles, context rules, trigger rules) are merged when
both computers changed them. Not synced: team macros (each computer subscribes
itself), history, saved variables, key remaps, the other per-computer settings -
and image files used by vision steps (keep them inside the synced folder and use
the same path everywhere, or re-capture them). Switching to another folder starts a
fresh merge. Edits made on two computers before either synced become a conflict
copy; an edit in the editor that is saved over a macro that sync changed meanwhile
wins at the next sync.

**Team libraries** live in `<folder>/keyboardmanager-team/`; who may publish is decided
by the folder's permissions (read-only access makes a subscriber-only member). Team
macros keep the library's name, hotkey and steps - except that a hotkey or
abbreviation already used on this computer is dropped, and whether a team macro is
enabled is your own choice, kept across updates. Leaving a library removes its team
macros from this computer; right-click > **Remove from team** on a macro you published
takes it out of the library, and the members' copies disappear at their next update.

**Remote control** listens on `127.0.0.1:8765` by default. Every request needs the
access token (`Authorization: Bearer <token>`, or `?token=` in the web panel's
address); anyone with the token can run your macros, so share it only with your own
devices and press **New token** if it leaked. With *other devices on my network* the
API is plain HTTP on the local network: use it only on networks you trust (your OS
firewall may ask once). Remote runs are triggers named `remote`: cooldowns and daily
limits apply, disabled macros and macros outside the active profile are refused, and
*Remote macros: Only favorites* narrows what can be run. API (JSON):

```
GET  /api/status     GET /api/macros     GET /api/history?limit=20
POST /api/run {"macro": "Name or id", "queue": false}
POST /api/stop | /api/pause | /api/resume | /api/toggle-pause
POST /api/profile {"name": "Work"}     POST /api/layer {"name": "nav", "one_shot": false}
POST /api/sync
```

```bash
keyboardmanager ctl status              # Idle / Running: ..., queue, profile, layer
keyboardmanager ctl run "Sign off" --queue
keyboardmanager ctl stop | pause | resume | sync | list | history
keyboardmanager ctl profile Work        # ctl layer nav --one-shot / ctl layer
keyboardmanager ctl url                 # the web panel's address (with the token)
```

`ctl` finds the running instance through the database's settings (`--db` for another
database) and needs remote control to be on.

**Appearance.** Qt cannot blur what lies behind a widget, so the glass is layered the
other way round: the window paints a smooth, colour-field backdrop tinted by the
accent (plus soft shadows under the floating panels) and every panel is a
translucent gradient with a bright top edge on top of it. Colours come from one
palette per light / dark variant and are checked for contrast (text at least 7:1,
secondary text and status colours at least 4.5:1 on every part of the backdrop,
for every accent). The glass look uses Qt's Fusion style underneath on every
platform (native styles ignore most of a style sheet); *Classic* restores the
platform style. On small screens the macro details scroll, and the gap between the
details and the steps can be dragged.

**Headless mode** (`keyboardmanager --headless`) runs hotkeys, abbreviations,
schedules, events, trigger rules, context rules, key remaps, sync and remote control
without any window, until Ctrl+C / SIGTERM - e.g. as a systemd user service or a
LaunchAgent of its own. What needs a window does not work there: *Ask for input*
fails, the clipboard is private to the process, and vision steps cannot see the
screen. Configure it with the normal application first (same `--db`).

## Install

**Ready-made application** (no Python needed) - download for your system from the
[releases](../../releases) page:

| System | File | First start |
| --- | --- | --- |
| macOS 14+ | `KeyboardManager-<version>-macos-<arch>.dmg` | Open the disk image, drag **KeyboardManager** to *Applications*. Unsigned builds: right-click the app > **Open** once, then allow the **Accessibility** and **Input Monitoring** prompts |
| Windows 10/11 | `KeyboardManager-<version>-windows-<arch>.zip` | Unpack anywhere, run `KeyboardManager.exe` (SmartScreen: *More info* > *Run anyway*) |
| Linux (X11) | `KeyboardManager-<version>-linux-<arch>.tar.gz` | Unpack and run `./KeyboardManager`. For a menu entry copy `keyboardmanager.desktop` to `~/.local/share/applications/` and an icon to `~/.local/share/icons/keyboardmanager.png` |

**As a Python package** (any platform, needs Python 3.10+):

```bash
uv tool install keyboardmanager      # or: pipx install keyboardmanager
keyboardmanager
```

## Run from source

```bash
uv sync
uv run keyboardmanager            # or: uv run python -m keyboardmanager
uv run keyboardmanager --db ./my.db --hotkey-backend pynput
uv run keyboardmanager --version
uv run keyboardmanager --background  # tray icon only (what "Start at login" uses)
uv run keyboardmanager --headless    # no window at all (a background service)
uv run keyboardmanager ctl status    # control the running instance
```

The database lives in the per-user data directory
(`~/Library/Application Support/KeyboardManager` on macOS, `%APPDATA%\KeyboardManager`
on Windows, `~/.local/share/KeyboardManager` on Linux) unless `--db` is given.

## Platform notes

* **macOS** (14 Sonoma or newer with the locked dependencies, Intel or Apple silicon):
  grant **Accessibility** and **Input Monitoring** to the app that launches Keyboard
  Manager (Terminal, iTerm, ...) in System Settings > Privacy & Security, then restart
  it. macOS gives no error without them (hotkeys never fire, recordings stay empty),
  so Keyboard Manager checks at start-up and shows which one is missing. Window
  titles of other apps and the vision steps (screen captures come back black or empty
  without it) additionally need **Screen Recording**; application names work
  without it. `cmd` is the Command key and `alt` the Option key: Option shortcuts such
  as `ctrl+alt+s` work for hotkeys and recordings even though Option changes the
  typed character (the current keyboard layout is read when hotkeys are registered).
  On laptops the F-keys of the stop / pause hotkeys may need `fn`. pynput's macOS
  keyboard listener calls main-thread-only keyboard-layout APIs from its own thread,
  which recent macOS versions punish by killing the process; Keyboard Manager uses a
  listener subclass that does that work on the main thread instead. The `keyboard`
  package crashes the interpreter on import on macOS, so it is not installed there
  and hotkeys always use pynput. Cmd+Q quits (asking about unsaved changes); closing
  the window keeps the app running in the menu bar.
  For macOS 12-13: `uv pip install . "PySide6<6.12"` (13) or `"PySide6<6.10"` (12),
  since newer PySide6 wheels need macOS 14.
* **Windows**: works out of the box; `cmd` means the Windows key. The `keyboard`
  package is used for hotkeys, with automatic fallback to pynput. Allowing remote
  control from the network makes Windows Defender Firewall ask once.
* **Linux**: X11 only (Wayland blocks global hooks). Needs the Qt X11 libraries
  (`libxcb-cursor0` and friends) when running from source. Application/window conditions
  need `xdotool`. The `keyboard` backend needs root, so pynput is the default.

## Macro file format

```json
{
  "format": "keyboardmanager-macros",
  "version": 1,
  "macros": [
    {
      "name": "Sign off",
      "hotkey": "ctrl+alt+s",
      "app_filter": "mail, outlook",
      "window_filter": "",
      "enabled": true,
      "schedule": {"kind": "weekly", "time": "17:30", "days": [0, 1, 2, 3, 4]},
      "event": "focus_gained",
      "abbreviation": ";sig",
      "folder": "Work/Email",
      "tags": ["email", "work"],
      "favorite": true,
      "speed": 1.5,
      "limits": {"max_seconds": 30, "cooldown_s": 60},
      "when_busy": "queue",
      "profiles": ["Work"],
      "layer": "nav",
      "actions": [
        {"type": "type_text", "params": {"text": "Best regards"}},
        {"type": "if", "params": {"kind": "time", "value": "09:00-17:00"},
         "children": [{"type": "key_press", "params": {"keys": "ctrl+enter"}}],
         "else_children": [{"type": "delay", "params": {"ms": 500}}]}
      ]
    }
  ]
}
```

Action types: `key_press`, `key_down`, `key_up`, `type_text`, `paste_text`,
`mouse_move`, `mouse_click`, `mouse_scroll`, `delay`, `repeat`, `while`, `break`, `if`,
`run_macro`, `wait_for_window`, `wait_for_image`, `click_image`, `focus_window`,
`stop`, `comment`, `set_variable`, `ask`, `set_clipboard`, `launch`, `notify`,
`script` (`{"code": "..."}`), `switch_profile`, `switch_layer`.
Parameters per type are defined in `src/keyboardmanager/core/actions.py`.

`schedule` is optional (omitted = not scheduled): `{"kind": "once", "at": "YYYY-MM-DD HH:MM"}`,
`{"kind": "weekly", "time": "HH:MM", "days": [0..6]}` (Monday = 0) or
`{"kind": "interval", "minutes": N}` (1 to 10080). `event` is optional:
`"app_start"`, `"focus_gained"` or `"focus_lost"` (the focus events need an
application / window-title filter). `hotkey` may be a sequence: `"ctrl+k ctrl+s"`.
`abbreviation` is optional: 2-32 characters without spaces. `folder` (`a/b` for nested
folders), `tags`, `favorite`, `speed` (0.1-10) and `limits` (`max_seconds`, `max_steps`,
`cooldown_s`, `max_runs_per_day`; 0 or missing = no limit) and `when_busy` (`"skip"`,
the default, or `"queue"`) are optional too. Conditions (`if`, `while`) may use
`"kind": "expression"` with an `"expression"` parameter, `set_variable` the mode
`"expression"`. `profiles` (a list of names) and `layer` are optional; image paths in
vision steps are not rewritten on import, so ship the image files along with the
macros.

## Layout

```
src/keyboardmanager/
  core/      no Qt: keys, models, actions (schema/validation), database, io,
             executor, recorder, hotkeys, backend (pynput), service, windowinfo,
             schedule (schedule model/timing), scheduler (runs due macros),
             events (run on focus changes / start-up), expander (abbreviations),
             remap (key remapping backends), variables (placeholders, calculator,
             saved variables), interaction (what macros ask of the UI), launcher,
             organize (folders, tags, speed), conflicts, preflight (the Check tab),
             limits, os_shortcuts (well-known system / app shortcuts),
             script (expression / script language), triggers (trigger rules and
             engine), debugger (breakpoints, stepping, simulated input),
             vision (image / pixel matching), windowcontrol (focusing windows),
             profiles (profile sets), context (context rules and their watcher),
             sync (folder sync between computers), team (team libraries),
             share (share codes), remote (HTTP API, web panel, client),
             autostart (start at login)
  gui/       PySide6: widgets, editor, recording, history, remapping, variables,
             uibridge (macro dialogs / clipboard on the GUI thread), macrotree,
             preflight (Check tab), runview (Run tab: live view, debugger, queue),
             triggers (Triggers tab), profiles (Profiles tab: profiles, layers,
             context rules), capture (screen grabs, the Capture helper),
             connect (Sync & Remote tab), instance (one running instance),
             theme (the Liquid Glass look: palettes, style sheet, backdrop), mainwindow
  app.py     entry point: window, --background, --headless, ctl
tests/       pytest (executor, recorder, service and database use fakes; GUI runs offscreen)
packaging/   appinfo.py (what goes into a bundle), PyInstaller spec, entitlements,
             .desktop entry
tools/       make_icons.py, package.py (build), smoke_bundle.py (check a build)
```

## Building the application

```bash
uv sync --group packaging
uv run python tools/package.py        # dist/: .app + .dmg, .exe + .zip, or binary + .tar.gz
uv run python tools/smoke_bundle.py   # check the built application starts
```

Bundles are built with PyInstaller for the system and CPU they are built on (a macOS
build made on Apple silicon does not run on Intel, and vice versa); the release
workflow builds all of them. The icon is drawn by `tools/make_icons.py`, everything
PyInstaller needs is in `packaging/appinfo.py` and `packaging/keyboardmanager.spec`.

* **macOS**: set `CODESIGN_IDENTITY="Developer ID Application: ... (TEAMID)"` before
  building to sign the app (`packaging/entitlements.plist` is applied). Without it the
  build works, but macOS warns on first launch and **may forget the Accessibility and
  Input Monitoring permission after every rebuild**, because those are remembered per
  signature; remove and re-add the app in System Settings when that happens. For
  distribution outside your own machine, notarise the signed `.dmg`:
  `xcrun notarytool submit --apple-id ... --team-id ... --wait dist/*.dmg` followed by
  `xcrun stapler staple dist/*.dmg`. The app is deliberately **not sandboxed**: a
  sandboxed app cannot send keystrokes to other applications.
* **Linux**: build on a machine that has the Qt X11 libraries installed
  (`libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libfontconfig1`), otherwise they
  are missing from the bundle and it will not start elsewhere.
* **Windows**: no signing is configured; SmartScreen warns until the binary gains
  reputation or you sign it yourself.

`uv build` produces the wheel and sdist for PyPI.

## Development

```bash
uv run pytest                                  # everything (~1190 tests, about 3 minutes)
uv run pytest --cov=keyboardmanager            # with coverage
uv run pytest tests/test_macos.py              # macOS behaviour, on any OS
uv run pytest -m native                        # only tests using the real OS APIs
xvfb-run -a uv run pytest                      # headless Linux (pynput needs an X display)
```

The suite never touches real input devices: a fake backend records injected input,
and hotkey hooks, listeners and the clock are faked. Qt runs on the offscreen
platform. Platform code for macOS, Windows and Linux is tested on every OS by
simulating `sys.platform` and the native modules (AppKit, Quartz, HIServices, Win32);
tests marked `native` additionally run against the real APIs where available.
CI (`.github/workflows/tests.yml`) runs the suite on Linux, Windows, macOS 14 and
the latest macOS, with Python 3.10 and 3.13.

| Tests | Cover |
| --- | --- |
| `test_keys`, `test_actions_catalog`, `test_models_actions` | key names, action validation, descriptions |
| `test_executor`, `test_service`, `test_schedule`, `test_triggers` | running, pause/resume, stop, hotkeys, sequences, scheduling, events |
| `test_recorder`, `test_backend`, `test_hotkeys`, `test_windowinfo` | input recording, injection, hotkey backends, active window |
| `test_variables`, `test_actions_more`, `test_launcher`, `test_gui_variables` | variables, the newer actions, opening apps, the GUI thread bridge |
| `test_recording_refine`, `test_os_shortcuts`, `test_limits_and_steps` | recording options and review, system shortcut clashes, limits, the step log |
| `test_organize`, `test_preflight`, `test_speed`, `test_gui_organize` | folders, tags, favorites, the pre-flight check, playback speed |
| `test_gui_undo_multiselect` | undo / redo, several steps and macros at once, copying steps |
| `test_script`, `test_script_steps` | the expression / script language (incl. what it refuses), scripts and expressions in macros, the debugger, simulated input, the live state |
| `test_queue_triggers`, `test_gui_run_triggers` | the run queue, trigger rules and engine, debug runs through the service, the Run and Triggers tabs, breakpoints |
| `test_vision`, `test_gui_capture` | image / pixel matching, the vision steps, focusing windows, screen grabs and the Capture helper |
| `test_profiles_layers`, `test_gui_profiles` | profiles, keymap layers, context rules, the Profiles tab and the toolbar switch |
| `test_sync_teams` | folder sync between two databases (edits, deletions, conflicts, clashes, damaged files, settings), team libraries, share codes |
| `test_theme` | the Liquid Glass palettes (contrast for every accent, light and dark), the style sheet, backdrop painting, switching appearance / accent, the window in every appearance |
| `test_remote_autostart`, `test_app_modes`, `test_gui_connect` | the remote control API over real local HTTP, start at login on every platform, one instance, --background, --headless, `ctl`, the Sync & Remote tab |
| `test_expander`, `test_remap`, `test_gui_remapping` | text expansion, key remapping (never applied to the real keyboard) |
| `test_macos` | Mac APIs, permissions, Option-key hotkeys/recording, dependencies |
| `test_database_io` | storage, migrations, import/export |
| `test_gui*`, `test_app` | window, editor, dialogs, tray, quitting, start-up |
| `test_integration` | the whole application with only the OS faked |
| `test_packaging` | distribution metadata, PyInstaller configuration, icons, build scripts |
| `test_native_smoke` | the real app in a child process: real listeners and event loop, toggling hotkeys / schedules and recording (skipped without a desktop session) |

After changing dependencies run `uv sync`: `uv run` installs what is missing but does
not remove packages that are no longer needed (such as `keyboard` on macOS).

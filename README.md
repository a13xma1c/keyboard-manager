# Keyboard Manager

A desktop application for building custom keyboard shortcuts, macros and automated
sequences. Built with Python, PySide6 (GUI), pynput (input injection, recording and
global hotkeys), the `keyboard` package (alternative hotkey backend, default on
Windows) and SQLite (storage).

## Features

| Feature | Where |
| --- | --- |
| Custom keyboard shortcuts | Give any macro a **Hotkey** (use *Capture* to press the combination) |
| Record keyboard and mouse | Editor > **Record...** (stop with the stop key, F9 by default) |
| Visual macro editor | Step tree with drag and drop, nesting, duplicate, reorder, per-step forms |
| Conditions and delays | **If / else** (active app, window title, time range, variable), **Delay** (+ random jitter), **Repeat**, **Set variable**, **Stop** |
| Application-specific shortcuts | *Application* / *Window title* filters (**Detect** fills them from the window you switch to). A specific macro beats a global one on the same hotkey |
| Import / export | JSON files via **Import...** / **Export...** (selected macro or all). Imports are validated; name clashes are renamed, clashing hotkeys are cleared |
| Global hotkeys | Active while the app runs (it keeps running in the system tray when the window is closed). Toggle in the toolbar. Schedules likewise only run while the app runs |
| Execution history | **History** tab: time, macro, trigger, status (incl. `skipped` scheduled runs), duration, steps run, message |
| Scheduled runs | Editor > **Schedule**: *Once* (date and time), *Days of the week* at a time (daily, weekdays, weekly...), or *Repeatedly* every N minutes. **Schedules** in the toolbar switches them all off |
| Pause / resume | **Pause** / **Resume** in the toolbar or tray menu, or the **pause hotkey** (default `ctrl+alt+f11`, changeable via *Pause hotkey...*, empty = none) |

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

Schedules use local time and are never caught up: runs missed while the app was
closed are dropped. A run that cannot start within 60 seconds of its time (another
macro still running, or the computer was asleep) is skipped and listed in History as
`skipped` with the reason; many missed *Repeatedly* runs count as one. *Repeatedly*
counts from when the schedule becomes active (app start, save, or Schedules switched
on). Disabled macros do not run, and *Application* / *Window title* filters apply as
for hotkeys: if another window has focus at that moment, the run is passed over
silently. After an import, the summary says how many macros will run on a schedule.

## Run

```bash
uv sync
uv run keyboardmanager            # or: uv run python -m keyboardmanager
uv run keyboardmanager --db ./my.db --hotkey-backend pynput
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
  titles of other apps additionally need **Screen Recording**; application names work
  without it. `cmd` is the Command key and `alt` the Option key: Option shortcuts such
  as `ctrl+alt+s` work for hotkeys and recordings even though Option changes the
  typed character (the current keyboard layout is read when hotkeys are registered).
  On laptops the F-keys of the stop / pause hotkeys may need `fn`. The `keyboard`
  package crashes the interpreter on import on macOS, so it is not installed there
  and hotkeys always use pynput. Cmd+Q quits (asking about unsaved changes); closing
  the window keeps the app running in the menu bar.
  For macOS 12-13: `uv pip install . "PySide6<6.12"` (13) or `"PySide6<6.10"` (12),
  since newer PySide6 wheels need macOS 14.
* **Windows**: works out of the box; `cmd` means the Windows key. The `keyboard`
  package is used for hotkeys, with automatic fallback to pynput.
* **Linux**: X11 only (Wayland blocks global hooks). Application/window conditions
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

Action types: `key_press`, `key_down`, `key_up`, `type_text`, `mouse_move`,
`mouse_click`, `mouse_scroll`, `delay`, `repeat`, `if`, `set_variable`, `stop`.
Parameters per type are defined in `src/keyboardmanager/core/actions.py`.

`schedule` is optional (omitted = not scheduled): `{"kind": "once", "at": "YYYY-MM-DD HH:MM"}`,
`{"kind": "weekly", "time": "HH:MM", "days": [0..6]}` (Monday = 0) or
`{"kind": "interval", "minutes": N}` (1 to 10080).

## Layout

```
src/keyboardmanager/
  core/      no Qt: keys, models, actions (schema/validation), database, io,
             executor, recorder, hotkeys, backend (pynput), service, windowinfo,
             schedule (schedule model/timing), scheduler (runs due macros)
  gui/       PySide6: widgets, editor, recording, history, mainwindow
  app.py     entry point
tests/       pytest (executor, recorder, service and database use fakes; GUI runs offscreen)
```

## Development

```bash
uv run pytest                                  # everything (~300 tests, ~20 s)
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
| `test_executor`, `test_service`, `test_schedule` | running, pause/resume, stop, hotkeys, scheduling |
| `test_recorder`, `test_backend`, `test_hotkeys`, `test_windowinfo` | input recording, injection, hotkey backends, active window |
| `test_macos` | Mac APIs, permissions, Option-key hotkeys/recording, dependencies |
| `test_database_io` | storage, migrations, import/export |
| `test_gui*`, `test_app` | window, editor, dialogs, tray, quitting, start-up |
| `test_integration` | the whole application with only the OS faked |

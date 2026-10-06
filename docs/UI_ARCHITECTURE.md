# Nika Core Web-style Windows UI and configurable keymap

Decision date: 2026-08-17.

## Shell
Nika Core will follow the proven Accessible Chess product shape: a normal Windows desktop application whose main surface is a local HTML/CSS/JavaScript application hosted in pywebview using the EdgeChromium/WebView2 renderer on Windows. The backend remains Python. No remote website is required for normal local operation.

This gives a modern site-like UI while preserving a normal executable/application window and a Python backend. pywebview 6.2.1 is the current baseline researched for Windows.

## Accessibility lesson from Accessible Chess
Accessible Chess demonstrated that correct HTML semantics alone are insufficient: the packaged WebView2 host boundary can still break UI Automation discovery. Nika therefore treats WebView2 host accessibility as an explicit boundary with tests. The release shell must ensure the real WebView2 control is focusable/attached to the top-level host and that the accessibility tree is discoverable. We will reuse/adapt the proven Accessible Chess WebView2 accessibility-host approach where compatible rather than rediscover it.

## Web semantics
Use native semantic HTML elements first: button, input, textarea, select, table, headings, landmarks, lists. ARIA is supplementary, not a replacement for native semantics. Dynamic status is exposed through appropriate live regions only where it does not create NVDA chatter. Focus changes are explicit and deterministic.

## Action Registry
Every user action has a stable ID independent of its visible label and shortcut. Examples: `task.create`, `task.pause`, `task.resume`, `agent.stop`, `nav.tasks`, `log.open`, `command.focus`.

The Action Registry owns label, description, category, default binding, whether it may be unbound, and handler. UI components call action IDs, not hard-coded key combinations.

## Keymap
All application-specific shortcuts are user-configurable. Defaults live in a versioned keymap file. User overrides live outside the installed program in the user configuration directory. The Settings > Keyboard page supports search, change, clear, restore default, export/import and conflict detection. Changes are validated before saving and are effective without editing source code.

Reserved/typing behavior: normal text-editing keys such as Ctrl+A/C/X/V/Z and navigation remain standard inside editable controls. Browser-specific WebView2 accelerators are disabled or intercepted where they conflict with application actions. No application shortcut may silently destroy standard text editing behavior.

## Shortcut conflict rules
- no two simultaneously active actions may own the same binding in the same scope;
- global/app scope, workspace scope and focused-control scope are explicit;
- conflicts are announced as text and block save unless the user deliberately reassigns;
- every default can be restored;
- critical actions remain available through menus/buttons even if unbound;
- destructive/high-impact actions cannot be made one-key instant operations without approval policy.

## Bridge
Prefer pywebview JS API bridge for local desktop calls with typed Nika facade methods. Do not expose arbitrary Python objects or unrestricted filesystem/shell calls to JavaScript. Validate every message crossing the bridge. Large/background tasks return IDs and update via state/events rather than blocking the UI thread.

## Packaging
For pywebview on Windows, PyInstaller is the first supported packaging path; Nuitka remains a fallback/alternative after measured compatibility. Build one-dir/standalone first for diagnosis, then optional one-file only after startup/accessibility/runtime assets are proven. Web assets are bundled locally. End users do not need Python installed.

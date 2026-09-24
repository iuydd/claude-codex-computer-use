# Claude + Codex Computer Use

[简体中文](README.zh-CN.md)

**Claude's Computer Use sucks? Use Codex's instead.**

Keep chatting with Claude. Let Codex's computer-use tools read the screen, click, and type in your desktop apps. This project connects Claude Code to the computer-use runtime already installed by Codex, including in local Code sessions in Claude Desktop.

- **Same Claude, different tools:** Claude still decides what to do; Codex's runtime operates the apps.
- **Choose how to approve app access:** show a confirmation dialog, or explicitly enable automatic access to all apps.
- **macOS and Windows:** requires a working Codex computer-use installation on the same machine. Windows support is experimental; Codex-specific browser automation is not included.

Under the hood, this unofficial MCP bridge forwards calls to `cua_repl` and handles its app-access requests.

**This repository contains only the bridge and installer.** It does not distribute OpenAI binaries, provide a standalone computer-use engine, or replace Claude's model.

## Requirements

- macOS or Windows, and Python 3.10 or newer. No third-party Python packages are required. Windows dialogs use the built-in Windows PowerShell and .NET Windows Forms.
- Claude Code, including local Code sessions in Claude Desktop.
- Codex Desktop with the `unified-computer-use` plugin and its runtime already installed and working.
- On macOS, the Accessibility and Screen Recording permissions required by that runtime.

The installer reads the installed plugin manifest under `~/.codex` (or `CODEX_HOME`) to locate the runtime. It does not download it. A missing or incompatible installation is an error. A macOS runtime cannot be copied to Windows; install and enable Codex's computer-use plugin on Windows first.

## Install

Download or clone this repository, then run these commands from its directory:

```sh
python3 install.py --dry-run
python3 install.py
```

On Windows, open PowerShell in the downloaded project folder and use:

```powershell
py -3 install.py --dry-run
py -3 install.py
```

Use `python` instead of `py -3` if your Python installation does not include the launcher. Run as your normal Windows user, so the installer updates that user's Claude configuration. For the commands below, replace `python3` with `py -3` on Windows. `~` means your user profile, usually `C:\Users\YOUR_NAME`.

This registers `cua_repl` in `~/.claude.json` and copies the bridge to `~/.claude/mcp-servers/claude-codex-computer-use/`. Existing configuration is backed up under `~/.claude/backups/`; unrelated MCP servers are preserved.

Start a new Claude Code session after installation. Existing sessions may keep the old MCP process and configuration. In the terminal client, `/mcp` lists the server.

If you want this bridge to replace Claude Desktop's built-in computer use, turn off **Settings → System → Enable computer use** in Claude Desktop. Labels may differ by version. The installer does not change that preference.

## Approval modes

By default, a matching app-access request opens a native dialog titled **Claude · Codex Computer Use**. Choose **Allow this request** or **Deny** in your system language. The dialog times out after 20 seconds and denies the request. It does not create a permanent allowlist.

The bridge's dialog and installer messages follow the macOS preferred language or Windows user UI language: English, Simplified Chinese, Traditional Chinese, or Japanese. Other languages fall back to English. If the system preference cannot be read, the locale environment is used. Restart the Claude Code session after changing your system language. Original runtime request text, technical logs, JSON keys, and argparse's built-in usage/error labels remain untranslated.

To explicitly allow access to every app without these dialogs:

```sh
python3 install.py --auto-approve-apps
```

This sets `CUA_BRIDGE_AUTO_APPROVE_APPS=1` for the MCP server. Claude can then read and operate apps, including apps that contain private information. Enable it only when that is the access you intend to grant.

Automatic approval is limited to the runtime's recognized app-access request format. Audio-recording requests, unrelated confirmations, and unknown MCP requests are not automatically approved. macOS permissions and Claude's own action policies remain separate.

To return to dialogs, reinstall without the flag and start a new session:

```sh
python3 install.py
```

## Try it

Ask Claude:

> Use cua_repl to read Finder's current interface. Do not change anything.

For a direct tool call, the first `js` invocation can be:

```js
let finder = await cua.getApp("com.apple.finder");
```

The runtime returns its API instructions. Follow those instructions before making subsequent calls. The bridge does not invent an additional UI API.

On Windows, ask Claude to list the available windows first, then read Notepad's interface without changing anything. Use the window ID returned by the runtime with `cua.getApp({windowId: ...})`; macOS app names and bundle IDs do not work on Windows. Windows input can activate the target window.

## Why a bridge?

The runtime sends app permissions through MCP `elicitation/create`. On the Claude Desktop build used during development, those requests were declined without displaying a prompt. A connection check or an app-inventory call could therefore succeed while opening an app failed with:

```text
Computer Use was not approved to use Finder
```

The bridge handles recognized app-access requests locally and forwards the rest of the protocol. In dialog mode it returns the human's actual choice; in automatic mode it applies the explicit installation setting.

## Limits

- **Native desktop apps only.** Codex-specific browser automation depends on Codex session metadata and is not enabled by this installer. A browser can still be a native desktop app; that is a different control surface.
- **Not an official integration.** Neither OpenAI nor Anthropic maintains or endorses this project. Runtime paths, request formats, and host behavior can change.
- **Not a portable runtime.** Moving the repository to another machine does not move the Codex installation or its permissions.
- **No host lifecycle integration.** The bridge cleans up its child process on exit, but does not reproduce Codex's host-specific turn hooks.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Runtime not found | Open Codex and verify its computer-use plugin is installed and works there. |
| Connected, but app access fails | In dialog mode, answer the native prompt within 20 seconds. Check that the new session loaded the bridge, not the unwrapped runtime. |
| Still seeing dialogs after enabling automatic access | Start a new Code session. Only recognized app-access requests are covered. |
| Breakage after a Codex update | Run the installer again to rediscover the installed runtime. |
| macOS denies screen or accessibility access | Grant the runtime the appropriate permissions in System Settings. The bridge cannot grant them. |

## Uninstall

```sh
python3 install.py --uninstall
```

Uninstallation removes only this tool's `cua_repl` entry. It leaves the bridge file and backups on disk and does not overwrite other settings. If you previously disabled Claude's built-in computer use, you can re-enable it in Claude Desktop.

## Development

```sh
python3 -B -m unittest discover -s tests -v
```

Tests cover permission routing, explicit automatic access, rejection and timeout behavior, protocol forwarding, shutdown, and installation. They do not require Codex, Claude, or a live desktop. CI runs on macOS, Ubuntu, and Windows. The bridge and installer tests also passed on Windows 11 ARM64 with Python 3.14. Real-runtime verification is separate: Finder and WeChat reads/screenshots and Claude Code tool calls were tested on macOS. The complete Claude → Codex → desktop workflow has not yet been verified on Windows, so Windows support remains experimental.

## License

The bridge and installer are [MIT licensed](LICENSE). The Codex runtime and other vendor software remain subject to their own licenses and terms.

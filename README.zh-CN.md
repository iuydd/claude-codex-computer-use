# Claude Codex Computer Use

[English](README.md)

让 macOS 上的 Claude Code 使用本机 Codex 已安装的 computer use runtime。

这是一个非官方 MCP 适配器：把工具调用交给 Codex 的 `cua_repl`，并处理应用访问授权。默认弹出 macOS 确认框，也可以显式开启所有应用访问自动允许。

**项目只包含适配器和安装程序，不分发 OpenAI 的二进制文件。** 它不能独立提供 computer use，也不会把 Claude 的模型换成 Codex。

## 运行要求

- macOS、Python 3.10 或更新版本，无需第三方 Python 库。
- Claude Code，支持 Claude Desktop 的本地 Code 会话。
- 已安装并能正常使用 `unified-computer-use` 插件的 Codex Desktop。
- runtime 所需的 macOS 辅助功能和屏幕录制权限。

安装程序从本机 Codex 插件配置中查找 runtime，不会下载 runtime。如果找不到可用安装，会报错退出。

## 安装

下载或 clone 项目，在项目目录运行：

```sh
python3 install.py --dry-run
python3 install.py
```

程序会把适配器复制到 `~/.claude/mcp-servers/claude-codex-computer-use/`，并在 `~/.claude.json` 中注册 `cua_repl`。原配置备份到 `~/.claude/backups/`，其他 MCP 配置保持不变。

安装后新开一个 Claude Code 会话，旧会话可能继续使用旧进程和配置。终端版可以通过 `/mcp` 查看服务器。

如果要替代 Claude Desktop 自带的 computer use，可以在 **Settings → System → Enable computer use** 关闭旧功能。不同版本的菜单名称可能不同。安装程序不会替你改这个开关。

## 授权方式

默认情况下，应用访问请求会弹出标题为 **Claude · Codex Computer Use** 的确认框。点击“允许本次请求”或“拒绝”；20 秒内未选择会自动拒绝。此模式不创建永久白名单。

如果明确希望所有应用访问都不再询问：

```sh
python3 install.py --auto-approve-apps
```

这会为 MCP 服务器设置 `CUA_BRIDGE_AUTO_APPROVE_APPS=1`。Claude 随后能够读取和操作各个应用，包括含有私人信息的应用。仅在你确实希望授予这些访问权限时开启。

自动允许只匹配 runtime 的应用访问授权格式。录音、其他类型的确认和未知 MCP 请求不会因此自动批准；macOS 系统权限与 Claude 自身的操作规则仍独立生效。

要恢复逐次确认，去掉参数重新安装，再新开会话：

```sh
python3 install.py
```

## 使用

可以直接对 Claude 说：

> 用 cua_repl 读取 Finder 当前界面，不要修改任何内容。

第一条 `js` 工具调用也可以直接使用：

```js
let finder = await cua.getApp("com.apple.finder");
```

runtime 会返回 API 使用说明，后续操作按说明执行。适配器不另外定义一套 UI 操作 API。

## 解决什么问题

Codex runtime 通过 MCP 的 `elicitation/create` 请求应用授权。在开发时测试的 Claude Desktop 版本中，这类请求没有显示确认窗口就被拒绝。因此，连接成功、能列出应用，并不代表能真正访问应用；调用可能报错：

```text
Computer Use was not approved to use Finder
```

适配器在本机处理识别到的应用授权请求，其他协议消息继续转发。确认模式传回用户实际点击的结果；自动模式使用安装时明确开启的授权设置。

## 限制

- 只启用桌面应用控制。Codex 专属浏览器通道依赖 Codex 会话信息，安装程序不会启用。通过桌面应用界面控制浏览器属于另一种控制方式。
- 这是非官方适配，OpenAI 和 Anthropic 均未维护或认可此项目。runtime 路径、请求格式和宿主行为可能随更新改变。
- 复制项目不会带走 Codex 的 runtime 或 macOS 权限。
- 适配器退出时会清理子进程，但没有复刻 Codex 宿主专用的回合结束 hooks。

## 排查问题

| 问题 | 检查方式 |
| --- | --- |
| 找不到 runtime | 打开 Codex，确认 computer use 插件已经安装且能正常使用。 |
| 连接成功，但应用访问被拒绝 | 确认模式下及时点击授权；检查新会话加载的是适配器而非直接启动 runtime。 |
| 自动允许后仍弹窗 | 新开 Code 会话；只有匹配的应用访问请求会自动通过。 |
| Codex 更新后失效 | 重新运行安装程序，刷新 runtime 路径。 |
| macOS 拒绝辅助功能或屏幕访问 | 在 System Settings 中授予 runtime 对应权限，适配器无法代授系统权限。 |

## 卸载

```sh
python3 install.py --uninstall
```

只移除本工具管理的 `cua_repl` 配置，保留磁盘上的适配器文件和备份，不覆盖其他设置。如果之前关闭了 Claude 自带的 computer use，可以自行重新打开。

## 开发与测试

```sh
python3 -B -m unittest discover -s tests -v
```

测试覆盖授权路由、自动授权、拒绝和超时、协议转发、进程退出以及安装行为，不依赖 Codex、Claude 或真实桌面。真实 runtime 的验证需要另外进行；开发时已在 macOS 测试 Finder 和微信的界面读取、截图，以及 Claude Code 的工具调用。

## 许可

适配器和安装程序采用 [MIT License](LICENSE)。Codex runtime 与其他厂商软件遵循各自的许可和条款。

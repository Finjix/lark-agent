# 飞书 Codex Desktop 任务机器人

Windows x64 本地常驻服务：飞书私聊或群聊 @机器人 提交任务 → 在本机运行的 Codex Desktop 中执行 → 流式卡片、最终回复、交付文件回传飞书。

## 运行

需要 Node.js ≥ 24（系统安装）、已登录并至少有过一个本地聊天的 Codex Desktop（验证版本 26.1007.9446.0）。

1. 飞书后台配置应用，见 [docs/feishu.md](docs/feishu.md)。
2. `.env.example` 复制为 `.env`，填凭证和访问范围。
3. 双击或运行 `register-mcp.cmd`，一键把浏览器和飞书 CLI 工具注册给 Codex（见下）；再运行 `set-prompt.cmd`，把系统提示词（`prompt/system.md`）写进 Codex 的全局指令，然后重启 Codex Desktop。
4. `start.cmd doctor` 自检，`start.cmd` 启动（首次自动 `npm ci`，每次先编译）。日志出现“飞书长连接已启动”即可用。

必须与 Desktop 在同一 Windows 用户下运行。数据库用 Node 内置 `node:sqlite`；依赖有飞书官方 SDK、`playwright-core`（浏览器引擎，使用系统已装的 Edge 或 Chrome，不下载浏览器）和官方飞书 CLI `@larksuite/cli`，都由 `npm ci` 按 lockfile 装进项目。

**系统提示词**：每轮不再向 Codex 注入任何说明。固定约定只有一句，写在 `prompt/system.md`：Codex 在名为 `lark-*` 的聊天目录里时，把要发给用户的文件放进 `output/`，用户自己的文件见 `lark_user_attachments` 里的路径。`set-prompt.cmd` 把它作为带标记的一段写入 `%USERPROFILE%\.codex\AGENTS.md`（Desktop“个性化”里的自定义指令文件）：保留你已有的内容，首次修改会备份为 `AGENTS.md.lark-agent.bak`，改了 `system.md` 后再运行一次即可更新，`set-prompt.cmd remove` 撤销。只在 `lark-` 目录里生效，不影响你自己的聊天。每轮唯一随消息携带的是附件清单（每条消息都不同）。

**MCP 注册**：Codex 通过本服务的 MCP 服务器 `lark_agent`（`http://127.0.0.1:8765/mcp`）使用浏览器和飞书 CLI。`register-mcp.cmd` 会（必要时先 `npm ci` 和编译）在 Codex 的 `config.toml`（`CODEX_HOME`，默认 `~/.codex`）里写入一段带标记的配置，幂等，不动其他内容，首次修改会备份为 `config.toml.lark-agent.bak`；若已有手写的同名条目则拒绝覆盖。`register-mcp.cmd remove` 撤销。这是唯一会修改 Codex 配置的操作，服务启动时不会改，只会在未注册时打印提醒。

## 目录

| 路径 | 内容 |
|---|---|
| `src` / `dist` | TypeScript 源码 / 编译产物（`start.cmd` 生成） |
| `dev` | 自动测试，`npm test` |
| `data` | 运行数据：SQLite 数据库、控制令牌、日志 |
| `user` | 只存用户数据，见下 |
| `.env` | 凭证和白名单，不入 Git |

```
user/
  李小明/                     每个飞书用户一个目录，显示名取自飞书，重名依次加 2、3（如 李小明2），分配后不再改变
    data/                     用户数据，跨聊天保留：浏览器 profile（cookie、登录态）、downloads、飞书 CLI 登录凭据（feishu-cli）
```

附件和交付文件不放在 `user/`，而放在 Codex Desktop 给每个聊天自己分配的工作目录里（`Documents\Codex\<日期>\lark-<会话ID>`，服务从 Desktop 的 `read_thread` 读到它）。Codex 在自己的工作目录里写文件不需要任何批准。两边交换文件都不规定命名：

```
<聊天工作目录>/
  input/     用户发来的附件，保留原文件名，重名加 (2)；有附件才会创建
  output/    Codex 要发给用户的文件；有产出才会创建
```

任务结束时，桥接程序把这个任务运行期间在 `output/` 里新建或改动过的文件（复制进来的文件按创建时间算）发给用户，再加上回复里链接的文件；每个任务最多自动带 10 个未链接的文件。要重发旧文件，让 Codex 再复制一份到 `output/` 即可。过期清理只删除该任务自己交付和接收的文件，不动文件夹和其他文件。

Desktop 里的聊天标题是 `显示名_yyyy_MMdd_HHmm_ssSS`。聊天被归档后会新建一个聊天和工作目录，但用户的 `data` 不变，所以浏览器和飞书 CLI 的登录不会丢。旧版本建在 `user/…/tasks`、`user/…/session` 下的目录不再使用，可以手工删除；旧任务按原路径过期清理。

## 行为

- **会话**：每个租户下每个用户固定一个 Desktop 聊天，消息按 FIFO 串行；不同用户并行，无全局并发上限。同一用户的多条消息共用一个聊天，Desktop 里一个聊天同一时刻只能跑一轮，所以同一用户的后一条会等前一条结束（无提示，卡片在开始执行时才出现）。
- **不可用时**：Desktop 不可用时，新消息不会入队，机器人只回一条“当前不可用，请稍后重新发送”；任务在提交给 Desktop 之前遇到 Desktop 掉线也直接失败，不再等待恢复。服务未启动期间发的消息，服务启动后若被飞书重新推送（发送时间早于启动时间）会被忽略，不会执行。仅服务重启后对已接收任务的恢复（核对 Desktop 原轮次）保留。聊天被归档或丢失时，下次消息自动新建会话。
- **输入**：用户文字原样提交（保留空白换行）。支持文字、图片、文件、图文混排；每条最多 10 个附件，单个 ≤ 30 MB。交付要求和附件清单走独立的应用上下文。新会话首次会做一次不回传的初始化。
- **任务关联**：用持久化的客户端请求 ID 和 Desktop 轮次 ID，相同原文连续发送也分别执行。提交结果不明时进入 `UNKNOWN`，核对原轮次，绝不自动重发。
- **回传**：卡片流式更新（每秒合并；超过 9 分钟改为整体更新），Codex 工作中标题栏显示计时。长回复转 Markdown 附件；文件只上传最终回复链接且位于任务目录内的；Codex 生成的图片自动发送（>10 MB 改发文件）。投递记录持久化，重启后继续。
- **命名**：用飞书显示名命名会话，需 `contact` 权限；查不到则保留默认名，任务照常执行，补权限后自动更新。
- **保留**：每条消息接收和交付的文件在任务终态、投递完成后保留 30 天（在聊天工作目录里）（`TASK_RETENTION_DAYS`）再清理；会话和任务记录不清理。默认无任务超时，交互请求 24 小时无响应则请求中断。
- **浏览器**：Desktop 内置浏览器所有用户共用一份 cookie，无法隔离，所以请在 Desktop 里关闭它（服务不再在提示词里强调）；网页操作由下面这些 MCP 工具完成，用法写在各工具的说明里，不额外注入提示词： `browser_open`、`browser_snapshot`、`browser_click`、`browser_screenshot` 等，由本服务里的无头 Playwright 浏览器（系统 Edge，无需下载浏览器）执行。每个用户一个独立 profile（`user/<用户名>/data/browser`），登录态跨任务保留，包括关闭浏览器后仍保留的会话 cookie；不同用户互不可见。仅允许 http/https，截图只保存在当前任务的交付目录。空闲 `BROWSER_IDLE_MS`（默认 10 分钟）后自动关闭浏览器进程。需要登录或验证码时，Codex 会在回复里向用户求助（可截图交付）。`.env` 中 `BROWSER_ENABLED=0` 可关闭，`BROWSER_CHANNEL`（msedge/chrome）或 `BROWSER_EXECUTABLE` 可更换浏览器。
- **飞书 CLI**：项目依赖里的官方 `@larksuite/cli`（`lark-cli`）的登录凭据默认存在系统钥匙串（Windows 是注册表 `HKCU\Software\LarkCli`），所有用户共用且无法重定向，所以不用它自带的登录。服务自己实现飞书设备授权，把每个用户的 token 存在 `user/<用户名>/data/feishu-cli/credentials.json`，过期前自动刷新；调用时由服务进程用环境变量注入给 `lark-cli`（并设 `STRICT_MODE=user`，只能以用户身份操作，不能借机器人的权限）。Codex 通过 MCP 工具 `feishu_cli`（参数是 `lark-cli` 的命令行参数）使用，拿不到应用密钥和 token。首次使用时 Codex 调用 `["auth","login"]`，把授权链接发给用户，用户点开授权后任务重试即可。`auth status` / `auth logout` 可查看和退出登录。登录的权限范围由 `.env` 的 `FEISHU_CLI_SCOPES` 指定，这些权限必须已在飞书开发者后台为应用开通并发布；`FEISHU_CLI_ENABLED=0` 可关闭，`FEISHU_CLI_PATH` 可指定其他位置的 `lark-cli`。

## 访问控制

`.env` 中：`ALLOWED_TENANTS`（整个企业开放，优先于用户名单，管理员也受限）→ 否则 `ALLOWED_USERS` / `ADMIN_USERS`（Open ID）。三者皆空则拒绝所有任务。`ALLOWED_CHATS` 可额外限制会话。改后重启。

查询、取消、交互仅限任务本人或管理员，不跨租户。所有任务共享 Desktop 的账号和系统权限，无操作系统级隔离，只对可信内部用户开放。浏览器 cookie 和飞书 CLI 凭据的隔离靠 MCP 服务器按 Codex 调用时附带的线程标识识别用户（模型无法改写，且必须有该线程正在执行的飞书任务），不是系统级沙箱：Codex 本身能读写 `user/` 和 `config.toml`，恶意的模型仍可绕过 MCP 直接读取他人文件。

## 指令

`/whoami`（Open ID 和租户）、`/help`、`/tasks`（最近 10 个）、`/status 任务ID`、`/cancel 任务ID`、`/approve 任务ID 请求ID`、`/deny 任务ID 请求ID`、`/answer 任务ID 请求ID 回答`（多问题用 `{"问题ID":"回答"}` JSON）。

## 运维

```bat
start.cmd doctor
start.cmd stop                  :: 通过本机令牌接口安全停止，也可 Ctrl+C
start.cmd backup                :: 备份数据库到 data/
start.cmd retry-delivery <任务ID|all>  :: 重试失败的投递，不重跑任务
```

健康检查 `http://127.0.0.1:8765/health`（`feishu.state` 为长连接实时状态：connecting、connected、reconnecting、failed），就绪 `/ready`（未就绪 503）。长连接失效时服务会自动重建。日志在 `data/logs`，保留 14 天；`.env` 可选 `CODEX_HOME`、`LOG_LEVEL`。重启会核对执行中任务并恢复队列和投递；没有自动重跑 `UNKNOWN` 任务的入口；对从未被 Desktop 确认执行轮次的 `UNKNOWN` 任务，任务本人或管理员可用 `/cancel` 结束它，释放该用户的队列（若请求已到达 Desktop，它仍可能继续执行）。Desktop 暂时不可用时尚未提交的任务会回到队列等待。

## 兼容性

Desktop 桥接使用内部协议，不检查 Desktop 版本；Desktop 升级后如协议有变化，连接或提交会失败并在日志、`start.cmd doctor` 中报错。适配器不修改 Desktop 的安装或聊天数据库；唯一对 Codex 配置的改动是 `config.toml` 里带标记的 `lark_agent` MCP 段。会话用 `create_thread` 初始化，业务轮次用 `thread-follower-start-turn` 提交。

单元测试不能替代实机验收：改动后请用飞书私聊和群聊验证卡片、附件回传，以及重启后对同一 thread/turn 的恢复。

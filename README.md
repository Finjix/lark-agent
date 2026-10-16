# 飞书 Codex Desktop 任务机器人

Windows x64 本地常驻服务：飞书私聊或群聊 @机器人 提交任务 → 在本机运行的 Codex Desktop 中执行 → 流式卡片、最终回复、交付文件回传飞书。

## 运行

需要 Node.js ≥ 24（系统安装）、已登录并至少有过一个本地聊天的 Codex Desktop（验证版本 26.1007.9446.0）。

1. 飞书后台配置应用，见 [docs/feishu.md](docs/feishu.md)。
2. `.env.example` 复制为 `.env`，填凭证和访问范围。
3. 双击或运行 `register-mcp.cmd`，一键把浏览器和飞书 CLI 工具注册给 Codex（见下），然后重启 Codex Desktop。
4. `start.cmd doctor` 自检，`start.cmd` 启动（首次自动 `npm ci`，每次先编译）。日志出现“飞书长连接已启动”即可用。

必须与 Desktop 在同一 Windows 用户下运行。数据库用 Node 内置 `node:sqlite`；依赖有飞书官方 SDK、`playwright-core`（浏览器引擎，使用系统已装的 Edge 或 Chrome，不下载浏览器）和官方飞书 CLI `@larksuite/cli`，都由 `npm ci` 按 lockfile 装进项目。

**MCP 注册**：Codex 通过本服务的 MCP 服务器 `lark_agent`（`http://127.0.0.1:8765/mcp`）使用浏览器和飞书 CLI。`register-mcp.cmd` 会（必要时先 `npm ci` 和编译）在 Codex 的 `config.toml`（`CODEX_HOME`，默认 `~/.codex`）里写入一段带标记的配置，幂等，不动其他内容，首次修改会备份为 `config.toml.lark-agent.bak`；若已有手写的同名条目则拒绝覆盖。`register-mcp.cmd remove` 撤销。这是唯一会修改 Codex 配置的操作，服务启动时不会改，只会在未注册时打印提醒。

## 目录

| 路径 | 内容 |
|---|---|
| `src` / `dist` | TypeScript 源码 / 编译产物（`start.cmd` 生成） |
| `dev` | 自动测试，`npm test` |
| `data` | 运行数据：SQLite 数据库、控制令牌、日志 |
| `user/<会话名>` | 用户会话目录。`tasks/<任务ID>` 放交付文件，`inputs/<任务ID>` 放入站附件，`browser` 是该用户独立的浏览器数据（cookie、登录态），`downloads` 放浏览器下载的文件，`feishu-cli` 是该用户的飞书 CLI 登录凭据 |
| `.env` | 凭证和白名单，不入 Git |

会话名即会话标题 `显示名_yyyy_MMdd_HHmm_ssSS`（北京时间，创建时固定，后续改名不移动目录）。

## 行为

- **会话**：每个租户下每个用户固定一个 Desktop 聊天，消息按 FIFO 串行；不同用户并行，无全局并发上限。聊天被归档或丢失时，下次消息自动新建会话。
- **输入**：用户文字原样提交（保留空白换行）。支持文字、图片、文件、图文混排；每条最多 10 个附件，单个 ≤ 30 MB。交付要求和附件清单走独立的应用上下文。新会话首次会做一次不回传的初始化。
- **任务关联**：用持久化的客户端请求 ID 和 Desktop 轮次 ID，相同原文连续发送也分别执行。提交结果不明时进入 `UNKNOWN`，核对原轮次，绝不自动重发。
- **回传**：卡片流式更新（每秒合并；超过 9 分钟改为整体更新），Codex 工作中标题栏显示计时。长回复转 Markdown 附件；文件只上传最终回复链接且位于任务目录内的；Codex 生成的图片自动发送（>10 MB 改发文件）。投递记录持久化，重启后继续。
- **命名**：用飞书显示名命名会话，需 `contact` 权限；查不到则保留默认名，任务照常执行，补权限后自动更新。
- **保留**：每条消息的 `tasks` 和 `inputs` 子目录在任务终态、投递完成后保留 30 天（`TASK_RETENTION_DAYS`）再清理；会话和任务记录不清理。默认无任务超时，交互请求 24 小时无响应则请求中断。
- **浏览器**：Desktop 内置浏览器所有用户共用一份 cookie，无法隔离。因此每轮上下文会禁止 Codex 使用内置浏览器，改让它使用 MCP 工具 `browser_open`、`browser_snapshot`、`browser_click`、`browser_screenshot` 等，由本服务里的无头 Playwright 浏览器（系统 Edge，无需下载浏览器）执行。每个用户会话一个独立 profile（`user/<会话名>/browser`），登录态跨任务保留，包括关闭浏览器后仍保留的会话 cookie；不同用户互不可见。仅允许 http/https，截图只保存在当前任务的交付目录。空闲 `BROWSER_IDLE_MS`（默认 10 分钟）后自动关闭浏览器进程。需要登录或验证码时，Codex 会在回复里向用户求助（可截图交付）。`.env` 中 `BROWSER_ENABLED=0` 可关闭，`BROWSER_CHANNEL`（msedge/chrome）或 `BROWSER_EXECUTABLE` 可更换浏览器。
- **飞书 CLI**：项目依赖里的官方 `@larksuite/cli`（`lark-cli`）的登录凭据默认存在系统钥匙串（Windows 是注册表 `HKCU\Software\LarkCli`），所有用户共用且无法重定向，所以不用它自带的登录。服务自己实现飞书设备授权，把每个用户的 token 存在 `user/<会话名>/feishu-cli/credentials.json`，过期前自动刷新；调用时由服务进程用环境变量注入给 `lark-cli`（并设 `STRICT_MODE=user`，只能以用户身份操作，不能借机器人的权限）。Codex 通过 MCP 工具 `feishu_cli`（参数是 `lark-cli` 的命令行参数）使用，拿不到应用密钥和 token。首次使用时 Codex 调用 `["auth","login"]`，把授权链接发给用户，用户点开授权后任务重试即可。`auth status` / `auth logout` 可查看和退出登录。登录的权限范围由 `.env` 的 `FEISHU_CLI_SCOPES` 指定，这些权限必须已在飞书开发者后台为应用开通并发布；`FEISHU_CLI_ENABLED=0` 可关闭，`FEISHU_CLI_PATH` 可指定其他位置的 `lark-cli`。

## 访问控制

`.env` 中：`ALLOWED_TENANTS`（整个企业开放，优先于用户名单，管理员也受限）→ 否则 `ALLOWED_USERS` / `ADMIN_USERS`（Open ID）。三者皆空则拒绝所有任务。`ALLOWED_CHATS` 可额外限制会话。改后重启。

查询、取消、交互仅限任务本人或管理员，不跨租户。所有任务共享 Desktop 的账号和系统权限，无操作系统级隔离，只对可信内部用户开放。浏览器 cookie 和飞书 CLI 凭据的隔离靠 MCP 服务器按 Codex 调用时附带的线程标识识别用户（模型无法改写，且必须有该线程正在执行的飞书任务），不是系统级沙箱：Codex 本身能读写 `user/` 和 `config.toml`，恶意的模型仍可绕过 MCP 直接读取他人文件。聊天被归档后会新建会话目录，浏览器登录态和飞书 CLI 登录需要重新做一次。

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

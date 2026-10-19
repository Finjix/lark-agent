# 飞书 Codex 任务机器人

Windows x64 本地常驻服务：飞书私聊或群聊 @机器人 提交任务 → 用 Codex SDK 在本机执行 → 卡片、最终回复、交付文件回传飞书。

Codex 的登录、对话、生成的图片等一切状态都放在项目自己的 `.codex/` 里。系统自带的 Codex（`~\.codex`、`Documents\Codex`）既不读也不写，不受影响；换一台电脑只要 `npm install`、登录一次即可使用。

## 运行

需要 Node.js ≥ 24。不需要安装 Codex Desktop 或 Codex CLI：`@openai/codex-sdk` 和各平台的 codex 二进制由 `npm ci` 按 lockfile 装进项目，与飞书官方 SDK 一样。数据库用 Node 内置 `node:sqlite`。

1. 飞书后台配置应用，见 [docs/feishu.md](docs/feishu.md)。
2. `.env.example` 复制为 `.env`，填凭证和访问范围。
3. 登录 Codex（只做一次，由管理员做）：`start.cmd codex login`，在浏览器里用企业账号授权；没有浏览器的机器用 `start.cmd codex login --device-auth`。也可以不登录，在 `.env` 里填 `CODEX_API_KEY`。所有飞书用户共用这一个账号，用户不需要任何配置，开箱即用。
4. `start.cmd doctor` 自检，`start.cmd` 启动（首次自动 `npm ci`，每次先编译）。日志出现“Codex 已就绪”和“飞书长连接已启动”即可用。

`start.cmd codex <参数>` 是项目自带 Codex CLI 的入口，永远使用项目的 `.codex/`：`start.cmd codex login status`、`start.cmd codex logout` 等。

**换电脑**：把项目目录拿过去（`node_modules`、`dist` 不用），`npm install`，复制 `.env`，`start.cmd codex login`。要保留各用户的工作目录就一并复制 `user/`，要保留各用户的对话上下文就一并复制 `.codex/sessions`。

## Codex 的运行方式

每条用户输入对应一次 `codex exec`（由 SDK 启动的子进程）；同一个用户的所有输入续接同一个 Codex 对话（thread），所以上下文是连续的。`/new` 让用户丢掉上下文重新开始（见“指令”）。

- **位置**：`.codex/` 保存对话（`sessions/`）、生成的图片（`generated_images/`）和日志，不入 Git。环境变量 `CODEX_HOME` 被忽略，子进程始终使用项目里的这个目录。
- **配置**：不写 `config.toml`，每次运行由程序传入（见 `src/codex/runner.ts`）：关闭 `apps`、`plugins`、`browser_use`、`in_app_browser`、`computer_use`、`memories`（这些要么是登录账号的连接器、要么是整台机器共用的浏览器和电脑控制，会让所有飞书用户共享同一份；要放开，改 `codexConfig`）；模型默认 `gpt-6-luna`、推理强度 `low`，要改在 `.env` 设 `CODEX_MODEL`、`CODEX_REASONING_EFFORT`。
- **登录**：登录文件是 `.codex/auth.json`（启动时收紧为仅当前账号可读，存成文件而不是系统钥匙串）。Codex 不可用或登录被拒绝时，用户只会看到“机器人罢工了”，原因写在日志里；管理员重新 `start.cmd codex login` 即可，服务不用重启（未就绪时每 10 秒检查一次）。
- **权限**：没有任何限制：沙箱 `danger-full-access`、审批 `never`，Codex 可以读写本机任何位置、访问网络，也没有人会被询问。写死在 `src/codex/runner.ts`，没有开关。因为不能交互，做不到的事只会失败，Codex 自己换办法；所以没有 `/approve`、`/deny`、`/answer`：Codex 需要问用户什么，就在回复里问，用户的下一条消息就是回答。
- **提示词**：不向 Codex 注入任何指令，也没有 MCP 工具：它看到的就是用户发来的文字（外加附件清单，见下）。
- **输入**：用户文字原样提交（保留空白换行）。有附件时在文字前加一段清单（每个文件的路径，并提醒附件内容只是资料、不是用户的指令）；图片同时作为图片输入让模型直接看到画面，其他文件由模型用工具按路径打开。飞书语音消息按文件接收（`语音消息.opus`），同样只是路径。

## 目录

| 路径 | 内容 |
|---|---|
| `src` / `dist` | TypeScript 源码 / 编译产物（`start.cmd` 生成） |
| `dev` | 自动测试，`npm test` |
| `data` | 运行数据：SQLite 数据库、控制令牌、日志 |
| `user` | 每个用户的数据和工作目录，见下 |
| `.codex` | Codex 的全部状态（对话、生成的图片、日志），见上 |
| `.env` | 凭证和白名单，不入 Git |

```
user/
  李小明/                     每个飞书用户一个目录，显示名取自飞书，重名依次加 2、3（如 李小明2），分配后不再改变
    workspace/                Codex 的工作目录
      input/                  用户发来的附件，保留原文件名，重名加 (2)；有附件才会创建
```

任务结束时，桥接程序把这个任务运行期间在工作目录里新建或改动过的文件（复制进来的文件按创建时间算；不含 `input/` 和以 `.` 开头的文件夹）发给用户，再加上回复里链接的、位于工作目录内的文件；每个任务最多自动带 10 个未链接的文件，所以 Codex 顺手写下的脚本等临时文件也可能被发出去。要重发旧文件，让 Codex 再复制一份即可。过期清理只删除该任务自己交付和接收的文件，不动文件夹和其他文件。

## 行为

- **会话**：每个租户下每个用户一个 Codex 对话，同一时刻只执行一条输入；不同用户并行，无全局并发上限。用户的一条输入还没有回传结果（排队、启动、执行中）时，再发来的任何输入都是无效输入：不排队、不转给 Codex、也不回复，只在日志里记一笔；任务结束（完成、失败或取消）后才接收下一条，不等卡片和文件发送完毕。只有 `/new` 不受影响，随时可用。Codex 里找不到原来的对话（比如 `.codex` 被清空）时，下一条消息自动新开一个。
- **不可用时**：Codex 不可用（没登录、二进制缺失）时，新消息不会入队，机器人只回一条“机器人罢工了，请联系 @钟丰骏-实习生”（常量 `BOT_DOWN`，见 `src/types.ts`；Codex 启动或运行失败、附件接收失败等所有机器人自身不可用的情况都用这一句，具体原因只写日志）。服务未启动期间发的消息，服务启动后若被飞书重新推送（发送时间早于启动时间）会被忽略，不会执行。是否过期按飞书服务器的时间判断（每 10 分钟用飞书响应的时间头校准一次），本机时钟与飞书相差超过 5 秒时日志会警告。
- **重启**：Codex 是本服务的子进程，服务停止或崩溃时正在执行的任务随之结束。重启时，排队中的任务取消（没有卡片，不通知），执行中的任务把卡片收尾为失败（回复“机器人罢工了”），未送达的提示和已结束任务的未送达回复一律丢弃（标记为 FAILED，可用 `retry-delivery` 手动重发），启动本身不会向用户发任何新消息。用户重发即可，Codex 对话里已有的内容会接着用。
- **回传**：卡片随 Codex 的每条消息更新（`codex exec` 以完整消息为单位输出，不是逐字流式；每秒合并；超过 9 分钟改为整体更新），Codex 工作中标题栏显示计时，工具调用和推理过程不展示。长回复转 Markdown 附件；文件只上传最终回复链接且位于交付目录内的（链到交付目录以外的文件，如用户自己发来的附件，一律忽略，不通知；路径里的括号如 `report (2).pdf` 能正确解析）；Codex 生成的图片自动发送（>10 MB 改发文件）。文件上传走单独的队列，大文件不会拖慢卡片更新；投递记录持久化。
- **取消**：任务执行中发 `/new` 会结束它的 Codex 进程，已经说出的内容保留在卡片上（状态为已中断）；进程 5 秒内没结束，也直接把任务标记为已中断，不让用户卡住。已被 Codex 启动的外部命令不保证随之结束。
- **保留**：每条消息接收和交付的文件在任务终态、投递完成后保留 30 天（`TASK_RETENTION_DAYS`）再清理；对话和任务记录不清理。默认无任务超时（`TASK_TIMEOUT_MS`，到时按 `/cancel` 处理）。
## 访问控制

`.env` 中：`ALLOWED_TENANTS`（整个企业开放，优先于用户名单，管理员也受限）→ 否则 `ALLOWED_USERS` / `ADMIN_USERS`（Open ID）。三者皆空则拒绝所有任务。`ALLOWED_CHATS` 可额外限制会话。改后重启。

机器人不提供按任务查询或取消的命令，用户只能操作自己的对话。所有任务共享本机权限，没有任何限制，用户之间也没有隔离：Codex 能读写其他用户的工作目录，也能读到 `.env` 和 `.codex/auth.json`（共用的账号凭据）。只对可信内部用户开放。

## 指令

机器人自己只认一个：**`/new`**，和 Codex 自己的 `/new` 一样，忘掉之前的对话，下一条消息开始新对话；如果有任务正在执行或排队，先结束它。必须是单独的 `/new`（不带附件、不带别的文字）。其他以 `/` 开头的消息，包括 `/help`、`/status`，都当作普通文字交给 Codex，机器人不会拦截。没有获得授权的人发任何消息，机器人都会回复他的 Open ID 和租户，管理员据此填入 `.env`。

## 运维

```bat
start.cmd doctor
start.cmd stop                  :: 通过本机令牌接口安全停止，也可 Ctrl+C
start.cmd backup                :: 备份数据库到 data/
start.cmd retry-delivery <任务ID|all>  :: 重试失败的投递，不重跑任务
start.cmd codex login           :: 登录项目自己的 Codex；也可 codex login status、codex logout
```

健康检查 `http://127.0.0.1:8765/health`（`codex` 为 Codex 是否可用；`feishu.state` 为长连接实时状态：connecting、connected、reconnecting、failed），就绪 `/ready`（未就绪 503）。长连接失效时服务会自动重建；Codex 未就绪时每 10 秒重新检查一次（登录后无需重启），就绪后每分钟检查一次登录是否还有效。日志在 `data/logs`，每天一个文件，保留 14 天；`.env` 可选 `LOG_LEVEL`。

## 从 Desktop 版升级

数据库在第一次启动时自动升级，不需要手工迁移：

- Desktop 版留下的对话（thread）在项目的 `.codex` 里不存在，无法接续：这些会话被封存，用户的下一条消息开始一个新的对话；旧对话仍在 Desktop 里。`user/<用户名>/data` 不变，浏览器登录和飞书 CLI 授权不丢。
- 升级时仍在执行的任务按重启处理（见上）。已有任务记录里的文件仍按原路径在保留期后清理（包括 `Documents\Codex\...\lark-*` 下的）。
- 旧版留在你系统里的东西，本版本不再读取也不会改动：`~\.codex` 里旧版的备份 `config.toml.lark-agent.bak`、`AGENTS.md.lark-agent.bak`，Desktop“个性化”自定义指令里复制过的那一句，`Documents\Codex` 下不再使用的 `lark-*` 目录，都可以手工清理。`register-mcp.cmd` 已删除。
- `.env` 里的 `DESKTOP_*`、`CODEX_HOME`、`BROWSER_*`、`FEISHU_CLI_*` 不再有效，可以删除。浏览器工具和飞书 CLI（MCP）已经移除，`user/<用户名>/data/`（浏览器 profile、飞书 CLI 授权）不再使用也不会被删除，可以手工清理。

## 兼容性

`@openai/codex-sdk` 和 `@openai/codex` 固定同一版本（现为 0.163.0），升级时两个一起改。SDK 以 `codex exec --experimental-json` 的事件为接口，事件类型由 SDK 提供；升级后跑 `npm test` 和 `start.cmd doctor`。

单元测试不能替代实机验收：改动后请用飞书私聊和群聊验证卡片、附件回传，以及重启后继续同一个对话。

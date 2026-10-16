# 飞书 Codex Desktop 任务机器人

Windows x64 本地常驻服务：飞书私聊或群聊 @机器人 提交任务 → 在本机运行的 Codex Desktop 中执行 → 流式卡片、最终回复、交付文件回传飞书。

## 运行

需要 Node.js ≥ 24（系统安装）、已登录并至少有过一个本地聊天的 Codex Desktop（验证版本 26.1007.9446.0）。

1. 飞书后台配置应用，见 [docs/feishu.md](docs/feishu.md)。
2. `.env.example` 复制为 `.env`，填凭证和访问范围。
3. `start.cmd doctor` 自检，`start.cmd` 启动（首次自动 `npm ci`，每次先编译）。日志出现“飞书长连接已启动”即可用。

必须与 Desktop 在同一 Windows 用户下运行。`.npmrc` 设了 `ignore-scripts=true`，因为 better-sqlite3 自带预编译二进制，无需 C++ 工具链。

## 目录

| 路径 | 内容 |
|---|---|
| `src` / `dist` | TypeScript 源码 / 编译产物（`start.cmd` 生成） |
| `dev` | 自动测试，`npm test` |
| `data` | 运行数据：SQLite 数据库、控制令牌、日志 |
| `user/<会话名>` | 用户会话目录。`tasks/<任务ID>` 放交付文件，`inputs/<任务ID>` 放入站附件 |
| `.env` | 凭证和白名单，不入 Git |

会话名即会话标题 `显示名_yyyy_MMdd_HHmm_ssSS`（北京时间，创建时固定，后续改名不移动目录）。

## 行为

- **会话**：每个租户下每个用户固定一个 Desktop 聊天，消息按 FIFO 串行；不同用户并行，无全局并发上限。聊天被归档或丢失时，下次消息自动新建会话。
- **输入**：用户文字原样提交（保留空白换行）。支持文字、图片、文件、图文混排；每条最多 10 个附件，单个 ≤ 30 MB。交付要求和附件清单走独立的应用上下文。新会话首次会做一次不回传的初始化。
- **任务关联**：用持久化的客户端请求 ID 和 Desktop 轮次 ID，相同原文连续发送也分别执行。提交结果不明时进入 `UNKNOWN`，核对原轮次，绝不自动重发。
- **回传**：卡片流式更新（每秒合并；超过 9 分钟改为整体更新），Codex 工作中标题栏显示计时。长回复转 Markdown 附件；文件只上传最终回复链接且位于任务目录内的；Codex 生成的图片自动发送（>10 MB 改发文件）。投递记录持久化，重启后继续。
- **命名**：用飞书显示名命名会话，需 `contact` 权限；查不到则保留默认名，任务照常执行，补权限后自动更新。
- **保留**：每条消息的 `tasks` 和 `inputs` 子目录在任务终态、投递完成后保留 30 天（`TASK_RETENTION_DAYS`）再清理；会话和任务记录不清理。默认无任务超时，交互请求 24 小时无响应则请求中断。

## 访问控制

`.env` 中：`ALLOWED_TENANTS`（整个企业开放，优先于用户名单，管理员也受限）→ 否则 `ALLOWED_USERS` / `ADMIN_USERS`（Open ID）。三者皆空则拒绝所有任务。`ALLOWED_CHATS` 可额外限制会话。改后重启。

查询、取消、交互仅限任务本人或管理员，不跨租户。所有任务共享 Desktop 的账号和系统权限，无操作系统级隔离，只对可信内部用户开放。

## 指令

`/whoami`（Open ID 和租户）、`/help`、`/tasks`（最近 10 个）、`/status 任务ID`、`/cancel 任务ID`、`/approve 任务ID 请求ID`、`/deny 任务ID 请求ID`、`/answer 任务ID 请求ID 回答`（多问题用 `{"问题ID":"回答"}` JSON）。

## 运维

```bat
start.cmd doctor
start.cmd stop                  :: 通过本机令牌接口安全停止，也可 Ctrl+C
start.cmd backup                :: 备份数据库到 data/
start.cmd retry-delivery <任务ID|all>  :: 重试失败的投递，不重跑任务
```

健康检查 `http://127.0.0.1:8765/health`，就绪 `/ready`（未就绪 503）。重启会核对执行中任务并恢复队列和投递；没有自动重跑 `UNKNOWN` 任务的入口。

## 兼容性

Desktop 桥接使用内部协议，**仅支持 `DESKTOP_SUPPORTED_VERSIONS` 中已验证的版本**；升级后需重新实机验收，不要直接扩大列表。适配器不修改 Desktop 的安装、配置或聊天数据库。会话用 `create_thread` 初始化，业务轮次用 `thread-follower-start-turn` 提交。

单元测试不能替代实机验收：改动后请用飞书私聊和群聊验证卡片、附件回传，以及重启后对同一 thread/turn 的恢复。

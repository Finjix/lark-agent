# 飞书 Codex Desktop 任务机器人

Windows x64 本地常驻服务。通过飞书私聊或群聊 @机器人提交任务，在正在运行的 Codex Desktop 中创建独立聊天和目录，回传进度卡片、最终回复和交付文件。

## 直接运行

完整离线包已经包含 Node.js 24.21.0 LTS、飞书 SDK、SQLite 原生模块、全部 npm 依赖和编译产物。解压后不需要安装系统 Node.js，也不需要执行 npm install。

1. 安装、登录并启动支持版本的 Codex Desktop，至少完成过一个本地聊天。当前验证版本：**26.1007.9446.0**。
2. 在飞书开发者后台配置企业自建应用，启用机器人，按 [飞书配置说明](docs/feishu.md) 开通权限、长连接和消息事件，并发布应用。
3. 复制 `.env.example` 为 `.env`，填入应用凭证和 `ALLOWED_USERS`；或使用项目自带 Node 运行 `scripts/setup.mjs`，在其显示的本机网址填写。
4. 双击 `doctor.cmd` 检查运行时、SQLite、Desktop 上下文及飞书凭证。
5. 双击 `start.cmd`。日志出现“飞书长连接已启动”后即可使用。

启动不下载依赖。实际飞书通信、模型任务和 Desktop 登录仍需要网络。服务须运行在与 Desktop 相同的 Windows 用户环境中。

## 使用

私聊直接输入任务；群聊需要 @机器人。首版接收文本任务，文件作为执行结果交付。

| 指令 | 用途 |
|---|---|
| `/whoami` | 获取本人的 Open ID，交管理员加入白名单 |
| `/help` | 帮助 |
| `/tasks` | 最近十个本人任务 |
| `/status 任务ID` | 查询状态和结果 |
| `/cancel 任务ID` | 取消排队任务，或请求中断执行 |
| `/approve 任务ID 请求ID` | 仅同意当前授权请求 |
| `/deny 任务ID 请求ID` | 拒绝当前请求 |
| `/answer 任务ID 请求ID 回答` | 回答补充问题；多问题用问题 ID 到回答的 JSON 对象 |

默认全局并发 4、每人 2。`UNKNOWN` 和等待确认的任务继续占用执行名额；同用户提交顺序保持 FIFO。查询、取消和交互仅限任务本人或管理员，且不得跨租户。

空白名单不会自动授权任何人。先向机器人发送 `/whoami`，将返回的 Open ID 写入 `.env` 的 `ALLOWED_USERS`，停止后重新启动。

## 项目内依赖和数据

- `runtime/node`：官方 Node.js 运行时及 npm，仅由项目脚本调用。
- `node_modules`：精确锁定的项目依赖，含 Windows SQLite 二进制。
- `dist`：预编译 JavaScript；`src`：TypeScript 源码。
- `data`：数据库、控制令牌、验收记录；后台运行日志在 `data/logs`。
- `.env`：本机凭证及授权名单，不进入 Git 或离线包。
- 每任务工作目录由 Desktop 原生创建并返回，通常位于“文档/Codex/日期/lark-任务ID/outputs”。实际路径保存在任务记录中。

普通文本回复超长时生成 Markdown 附件。仅上传最终回复明确链接、且真实路径位于所属任务目录内的文件；单个附件限 30 MB。原始模型推理、完整命令输出和内部事件不会进入卡片。

## 维护与开发

在项目目录使用以下命令，均通过项目自带 Node：

```bat
project.cmd run build
project.cmd test
start.cmd doctor
start.cmd stop
start.cmd backup
start.cmd retry-delivery all
project.cmd run pack:offline
```

`start.cmd stop` 会通过仅限本机、带随机令牌的控制接口安全停止。也可在启动窗口按 Ctrl+C。健康检查：`http://127.0.0.1:8765/health`；就绪检查：`/ready`，配置未齐时返回 503。

首次下载依赖或重建环境由开发人员进行，正常交付使用完整 ZIP。打包输出到 `releases`，带文件校验清单和 ZIP SHA-256；打包脚本拒绝覆盖既有暂存目录。仅克隆源码仓库不会自动获得被 Git 忽略的运行时和依赖。

登录自启动为可选项：执行 `scripts/autostart.ps1` 安装当前用户的启动快捷方式；加 `-Remove` 卸载。它以隐藏窗口启动监督脚本，异常退出后十秒重启；正常停止不重启。未自动修改 Windows 设置。Desktop 自身也需登录后启动。

数据库启用 WAL，服务重启会核对执行中任务并恢复队列和投递。飞书投递失败不会重跑任务。永久权限错误保留在 outbox 中，修复配置后可执行 `start.cmd retry-delivery 任务ID`（或明确使用 `all`）恢复投递；没有自动重跑 `UNKNOWN` 任务的入口。

默认关闭任务时限；交互请求 24 小时未响应会请求中断。任务文件默认保留 30 天，仅在任务终态、全部投递成功、目录所有权检查通过且 Desktop 归档成功后清理；任务元数据继续保留。

## 兼容性与边界

Desktop 桥接使用内部协议，**仅支持已验证版本**。更新后需要重新验收，不应直接扩大 `DESKTOP_SUPPORTED_VERSIONS`。适配器不修改 Desktop 安装文件、配置或聊天数据库；只读元数据用于发现已有有效调用上下文。

原生 `create_thread` 会直接启动第一轮任务，程序不会再提交第二次相同指令。创建或提交结果不明时进入 `UNKNOWN` 并等待核对。中断结果以匹配轮次的 Desktop 终态为准。

所有任务共享 Desktop 登录账号、系统权限和已连接服务。独立目录用于组织文件，不提供不同用户之间的操作系统权限隔离。请仅对受信任内部用户开放。

测试说明及尚未完成的实机验收见 [验收说明](docs/acceptance.md)。

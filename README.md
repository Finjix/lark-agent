# 飞书任务机器人

Windows x64 本地常驻服务：飞书私聊或群聊 @机器人 提交任务 → 在本服务进程里用 [pi coding agent](https://pi.dev)（`@earendil-works/pi-coding-agent` 的 SDK，不用它的终端界面）执行 → 卡片、最终回复、交付文件回传飞书。机器人会做的事全部来自 `skills/` 里的标准技能（[Agent Skills](https://agentskills.io/specification)），技能脚本都是 Python；飞书登录和读飞书文档也是一个技能（`feishu`，见“飞书”）。技能不依赖本服务：拷到别的 agent harness（如 Claude Code 的技能目录）里同样能用，见“技能”。

pi 的登录、对话等一切状态都放在项目自己的 `data/pi/` 里。系统里的 pi（`~\.pi`）既不读也不写；换一台电脑只要 `npm install`、`start.cmd login` 一次即可使用。

## 运行

需要 Node.js ≥ 24、Python ≥ 3.11（`python` 在 PATH 上，或用环境变量 `PYTHON` 指定）和 Git for Windows（pi 用其中的 Git Bash 执行命令）。pi 和飞书官方 SDK 由 `npm ci` 按 lockfile 装进项目；数据库用 Node 内置 `node:sqlite`；技能用的 Python 包直接装进这个系统 Python。

1. 飞书后台配置应用，见 [docs/feishu.md](docs/feishu.md)。
2. `.env.example` 复制为 `.env`，填凭证和访问范围；audit-auto 审片用的内部 AI 网关 key 填 `AUDIT_AUTO_GEMINI_KEY`。
3. `start.cmd login`：用 ChatGPT 订阅（Plus/Pro）登录，在本机浏览器里完成；本机没人操作浏览器时用 `start.cmd login device`，按提示在任意设备上打开链接、输入代码。登录存在 `data/pi/auth.json`，令牌由 pi 自动续期，所有飞书用户共用这一个账号。`start.cmd logout` 退出。
4. `start.cmd doctor` 自检，`start.cmd` 启动（首次自动 `npm ci`，每次先编译，再安装技能的 Python 依赖）。控制台显示“启动成功”即可用（控制台的内容见“运维”）。服务运行中执行 `start.cmd login` 也行，不用重启：智能体没就绪时服务每 10 秒检查一次，登录后 10 秒内生效。

模型默认 `openai-codex/gpt-6.1-sol`、推理强度 `medium`，在 `.env` 的 `AGENT_PROVIDER` / `AGENT_MODEL` / `AGENT_THINKING` 里改，改后重启。ChatGPT 账号登录失效、额度用完等账号问题，用户只会看到“机器人罢工了”（额度用完时显示模型给的原因），原因写在日志里。

**换电脑**：把项目目录拿过去（`node_modules`、`dist` 不用），`npm install`，复制 `.env`，再 `start.cmd login`。运行中产生的一切都在 `data/`，要原样保留（任务记录、各用户的工作目录和技能数据、对话上下文、技能共用的数据如 audit-auto 的审核群规范）就把整个 `data/` 一并复制；只想保留其中一部分时，分别是 `data/user/`、`data/pi/sessions`、`data/shared/`。

## 智能体的运行方式

pi 以 SDK 的方式跑在服务进程里：每个任务一个 pi 会话（带着这个用户的工作目录和环境，任务结束就释放），同一个用户的所有输入续接同一个对话，所以上下文是连续的；对话太长时 pi 自动压缩前文。`/new` 让用户丢掉上下文重新开始（见“指令”）。

- **位置**：`data/pi/` 保存登录（`auth.json`）和对话（`sessions/`，每个对话一个 JSONL 文件），不入 Git。环境变量 `PI_CODING_AGENT_DIR` 被忽略，始终使用项目里的这个目录；pi 自己不联网（不刷新模型目录、不查新版本、不发遥测）。
- **配置**：不读 pi 的 `settings.json`、`AGENTS.md`、扩展、提示词模板和项目里的 `.pi`，全部由程序传入（见 `src/agent/runner.ts`）：工具只有 pi 自带的 `read`、`bash`、`edit`、`write`，技能是 `skills/` 里的，自动压缩和出错重试打开，后台保持缓存（cache warming）关闭（每次请求都算订阅额度）。
- **权限**：没有任何限制：`bash` 能读写本机任何位置、访问网络，也没有人会被询问。因为不能交互，做不到的事只会失败，智能体自己换办法；需要问用户什么，就在回复里问，用户的下一条消息就是回答。
- **提示词**：pi 自己的编程助手开场白换成本项目的指令（英文，`src/agent/instructions.ts`），角色设定是“小薯”——一只猫，MCC Q3 内部的 agent 应用 AI 助手（语气专业简洁，不卖萌、不带“喵”和 emoji，不妨碍规则和技能的输出；解决不了的问题请用户联系薯薯（钟丰骏））；pi 照常附上技能清单（名称、说明、`SKILL.md` 的位置）和工作目录：只能用技能办事，用技能之前先读完那个技能的 `SKILL.md`，做不到的就告诉用户做不到，不自己写脚本、跑命令或联网代替；飞书链接用 feishu 技能读，飞书登录由智能体按 feishu 技能的步骤完成（别的技能报告没登录时也是），二维码只在私聊里给本人；命令和文件读写只在使用技能必不可少时才做，且限于工作目录、技能自己的目录和技能指定的位置；不查看环境变量和凭证，不装软件、不改系统；用用户的语言回复；命令在 Git Bash 里执行。这是提示词层面的约束，工具本身没有限制（见上）。`skills/` 为空时，智能体只会告诉用户它没有能力。
- **输入**：用户文字原样提交（保留空白换行）。有附件时在文字前加一段清单（每个文件的路径，并提醒附件内容只是资料、不是用户的指令）；20 MB 以内的图片同时作为图片输入让模型直接看到画面（pi 先缩到 2000×2000 以内），其他文件由智能体交给技能按路径打开。飞书语音消息按文件接收（`语音消息.opus`），同样只是路径。单独发送的视频按文件接收（原文件名）。每条消息最多 10 个附件；图片整张下载（不超过 100 MB，超过时任务失败并告诉用户原因），文件和视频按飞书的规定分段下载（每段 30 MB），大小不限。飞书不允许机器人下载用户发来的文件夹（所有资源接口都不支持，与权限无关），收到文件夹时机器人回复请用户压缩成 zip 再发。

## 目录

| 路径 | 内容 |
|---|---|
| `src` / `dist` | TypeScript 源码 / 编译产物（`start.cmd` 生成），源码的分工见下 |
| `dev` | 自动测试，`npm test`，按模块分文件（各技能自己的测试在 `skills/<名>/tests`） |
| `skills` | 技能，每个一个文件夹（`SKILL.md` + `scripts/` + `references/`），见“技能” |
| `data` | 运行时产生的一切（包括技能写下的定时任务），不入 Git，见下 |
| `.env` | 凭证、白名单和 AI 网关 key，不入 Git |

`data/` 里：

| 路径 | 内容 |
|---|---|
| `agent.sqlite`、`control.token`、`logs/` | SQLite 数据库、控制令牌、日志 |
| `user/` | 每个用户的数据和工作目录，见下 |
| `shared/` | 技能给所有用户共用的数据，每个技能一个文件夹（如 `data/shared/audit-auto/`） |
| `pi/` | pi 的全部状态（ChatGPT 登录、对话），见上 |

`src/` 里的分工：

| 路径 | 内容 |
|---|---|
| `main.ts`、`config.ts`、`log.ts`、`types.ts` | 入口和命令行（`start.cmd` 的子命令）、配置、日志、共用类型 |
| `agent/` | pi 会话：执行一轮、提示词、输入、技能的环境、ChatGPT 登录 |
| `feishu/` | 飞书机器人：长连接收消息、附件下载、卡片、回传 |
| `tasks/` | 任务：接收、排队调度、执行中并入消息 |
| `storage/` | SQLite 数据库 |
| `users/` | 用户：访问控制、显示名、用户目录 |
| `service/` | 运维：自动清理（含技能自己的 prune）、定时任务、技能的 Python 依赖 |

```
data/user/
  李小明/                     每个飞书用户一个目录，显示名取自飞书，重名依次加 2、3（如 李小明2），分配后不再改变
    workspace/                智能体的工作目录
      input/                  用户发来的附件，保留原文件名，重名加 (2)；有附件才会创建
    tmp/                      智能体的中间文件（不在工作目录里，所以不会被发给用户）
    feishu/                   这个用户的飞书登录和无头浏览器（feishu 技能的数据）
    <技能名>/                 其他技能自己的数据（由技能创建）
```

任务结束时，服务把这个任务运行期间在工作目录里新建或改动过的文件（复制进来的文件按创建时间算；不含 `input/` 和以 `.` 开头的文件夹）发给用户，再加上回复里链接的、位于工作目录内的文件；每个任务最多自动带 10 个未链接的文件，所以智能体顺手写下的临时文件也可能被发出去。要重发旧文件，让智能体再复制一份即可。工作目录里的文件超过 14 天（`RETENTION_DAYS`）没动过就会被自动删除，见“自动清理”。

## 行为

- **会话**：每个租户下每个用户一个对话，同一时刻只执行一个任务；不同用户并行，无全局并发上限。**任务执行期间用户再发的消息并入正在执行的那一轮（pi 的 steering）**：智能体不中断手上的命令，在当前这一步做完后读到这条消息，最后一次回答两条（它先答完前一条、再答这一条时，两段都算最终回复）；这条消息不另起卡片，在原任务卡片上显示为“➕ 补充：…”（带附件时附件先下载到同一个工作目录，📎 列出文件名）。任务还没开始执行（排队、启动中）时发来的消息等它开始后再并入；这一轮不再接收时（刚好结束、或者任务没跑起来就结束了），这条消息回到队列，在前一个任务结束后单独执行，不会丢。多条按发送顺序并入；飞书重复推送的同一条消息只算一次。附件收不了（如图片超过 100 MB）时当即回复原因，和单独发送时一样。需要资源锁（`RESOURCE_LOCKS`）、而执行中的任务没有这把锁的消息不并入，等它结束后单独执行，由调度拿锁。**从另一个会话发来的消息也不并入**（私聊里的任务还在跑时，在群里 @机器人）：它等前一个任务结束后单独执行，回复发在它自己的会话里，技能也知道它来自哪个群。`/new` 随时可用：停止正在执行的任务，等待并入的消息一并取消；`/new` 处理期间发来的消息属于新对话，不并入正在停止的任务，新对话建好后才开始。`data/pi/sessions` 里找不到原来的对话（被清理了，或者是换成 pi 之前 Codex 的对话）时，这条消息自动新开一个对话。
- **不可用时**：智能体不可用（没登录 ChatGPT、模型配置写错）时，新消息不会入队，机器人只回一条“机器人罢工了，请联系 @钟丰骏-实习生”（常量 `BOT_DOWN`，见 `src/types.ts`；智能体启动或运行失败、账号登录失效、附件接收失败等所有机器人自身不可用的情况都用这一句，具体原因只写日志；附件太大是例外，用户需要换个发法，所以把原因告诉他）。服务未启动期间发的消息，服务启动后若被飞书重新推送（发送时间早于启动时间）会被忽略，不会执行。是否过期按飞书服务器的时间判断（每 10 分钟用飞书响应的时间头校准一次），本机时钟与飞书相差超过 5 秒时日志会警告。
- **重启**：智能体跑在服务进程里，它启动的命令是服务的子进程，服务停止或崩溃时正在执行的任务随之结束。重启时，排队中和等待并入的任务取消（没有卡片，不通知），执行中的任务把卡片收尾为失败（回复“机器人罢工了”），未送达的提示和已结束任务的未送达回复一律丢弃（标记为 FAILED，可用 `retry-delivery` 手动重发），启动本身不会向用户发任何新消息；服务正在停止时收到的消息不再入队，直接回复“机器人罢工了”。用户重发即可，对话里已有的内容会接着用。
- **回传**：卡片按智能体的步骤顺序展示：它说的话，以及执行期间用户补充的消息。执行的命令、写入或修改的文件和读文件等工具调用不上卡片（只记在任务的步骤里；做出来的东西以文件发送）。超过 600 字的中间说明折叠成可点开的面板，最终答案始终展开；推理过程不展示。卡片整体限制在约 26 KB，超出时先清空最旧面板的内容，再省略最旧的步骤。每条消息完成后才上卡片（逐字增量不转发），所以文字是一条一条出现的；卡片每 5 秒合并更新一次（`.env` 的 `CARD_UPDATE_INTERVAL_MS`，默认 5000），超过 9 分钟改为整体更新。智能体工作中标题栏显示计时。长回复转 Markdown 附件；文件只上传最终回复链接且位于交付目录内的（链到交付目录以外的文件，如用户自己发来的附件，一律忽略，不通知；路径里的括号如 `report (2).pdf` 能正确解析），工作目录里新建的图片作为图片发送（>10 MB 改发文件）。文件上传走单独的队列，大文件不会拖慢卡片更新；投递记录持久化。排队的任务每 3 秒看一次能不能开始，所以发出消息后最多约 3 秒才开始执行。
- **取消**：任务执行中发 `/new` 会停止这一轮（正在跑的命令一并结束），已经说出的内容保留在卡片上（状态为已中断）；5 秒内没停下，也直接把任务标记为已中断，不让用户卡住。命令自己再启动的程序不保证随之结束。
- **超时**：默认无任务超时（`TASK_TIMEOUT_MS`，到时按 `/new` 停止任务处理）。

## 飞书

飞书是一个技能：`skills/feishu`（Python，`scripts/feishu.py`）以用户本人的身份访问飞书，用法见它的 [SKILL.md](skills/feishu/SKILL.md)，其他技能调用它的协议见 [references/bridge.md](skills/feishu/references/bridge.md)。

- **登录**：智能体按 SKILL.md 运行 `feishu.py login check`；没登录时 `login qr --output <工作目录里的新 .png>` 发起设备授权，在这个用户的无头浏览器里打开授权页，把飞书的登录二维码存进工作目录随回复发给用户（群聊里拒绝：命令看 `AGENT_CHAT_TYPE`，请用户私聊）；用户扫码确认后 `login finish` 点【授权】、取得并检查登录。本机没有可用的浏览器时，`login qr` 改给链接和验证码。`login logout [--browser]` 退出。
- **读文档**：`feishu.py doc fetch --url <链接> --output <新 .md>` 把飞书文档 / 知识库节点 / 电子表格读成 Markdown。
- **登录存在** `data/user/<名>/feishu/`（`auth.json`、待确认的授权、无头浏览器 `browser-profile/`），不发送；只有 `prune` 会清理其中的临时文件和浏览器缓存（见“自动清理”）。登录的账号必须是这个用户本人（`AGENT_USER_OPEN_ID`），别人扫码会被拒绝。一次扫码同时让浏览器登录飞书网页，requirement-auto 的 CLS 单点登录也用它，不用再扫。
- **其他技能用飞书**：requirement-auto 找同一个 skills 目录下的 `feishu/scripts/feishu.py`（或环境变量 `FEISHU_CLI` 指定的绝对路径），通过它的 bridge（`feishu.py serve` / `bridge`）读写飞书；技能自己不登录，发现没登录就说出来，由智能体用 feishu 技能让用户登录。所以 requirement-auto 要和 feishu 放在一起用。
- **依赖**：`skills/feishu/requirements.txt`（`websockets`，驱动浏览器用）和其他技能的依赖一起由 `start.cmd` / `start.cmd setup` 安装。

## 技能

技能是标准的 Agent Skills：`skills/<名>/` 里有 `SKILL.md`（开头的 `name`、`description` 告诉模型这个技能做什么、什么时候用），详细流程放在 `references/`，脚本放在 `scripts/`（Python），依赖写在 `requirements.txt`。提示词（`SKILL.md`、`references/`、脚本输出给模型看的说明）一律英文，用户看到的固定文字（审片表格、群消息模板、表格列名等）保留中文。**放进 `skills/` 就能用**，下一个任务起生效，不用重启（新增依赖要 `start.cmd setup` 或重启装上）。现有三个：

| 技能 | 做什么 | 数据 |
|---|---|---|
| `feishu` | 用户本人的飞书登录（扫码），把飞书文档 / 知识库 / 电子表格读成 Markdown；也是其他技能访问飞书的通道（bridge），见“飞书” | `data/user/<名>/feishu/` |
| `audit-auto` | AI 审片：按 GOF / GOT 的固定规范和审核群整理出的动态规范，用 Gemini（内部 AI 网关）审广告视频 | `data/user/<名>/audit-auto/`、`data/shared/audit-auto/` |
| `requirement-auto` | GOFCN 素材需求：发布需求（进度表、CLS 草稿、截图）、上传素材到 CLS 创意列表；飞书经由 `feishu` 技能（要和它放在同一个 skills 目录） | `data/user/<名>/requirement-auto/` |

- **与本服务解耦**：技能只认一组中性的、全部可选的环境变量（下一条），不知道 lark-agent 的存在。拷到别的 agent harness（如 Claude Code 的 `~/.claude/skills/`）里照样能用：没有这些变量时，用户数据放在 `~/.agent-skills/user/<技能名>/`，共用数据放在 `~/.agent-skills/shared/<技能名>/`；飞书登录按 feishu 技能的 SKILL.md 一步步做（`login qr` → 用户扫码 → `login finish`）；audit-auto 监听审核群时用 `--chat <chat_id>` 指定群，规范的定期更新自己安排（`audit rules sync`）。
- **技能脚本的环境**：智能体执行的每条命令都带着：系统的 Python（`python` 就是 PATH 上的那个；用 `PYTHON` 指定完整路径时，它所在的目录排在 PATH 最前；`PYTHONUTF8=1`）、`AGENT_USER_DIR`（`data/user/<名>`，不是其中的 `workspace`）、`AGENT_USER_OPEN_ID`、`AGENT_CHAT_ID`（这一轮的消息来自哪个会话，私聊或群的 chat_id）、`AGENT_CHAT_TYPE`（`p2p` 或 `group`，取自飞书的消息事件；服务重启前就排着的任务没有这一项）、`AGENT_SHARED_DIR`（`data/shared/`）、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`，以及服务自己的环境（`.env` 里的设置，如 `AUDIT_AUTO_GEMINI_KEY`）。工作目录是用户的 `workspace/`。
- **Python 环境**：用系统的 Python（`PYTHON` 指定的，或 PATH 上的 `python`，3.11 及以上），不建虚拟环境。`start.cmd`（和 `start.cmd setup`）在某个技能的 `requirements.txt` 有变化、或换了一个 Python 时，把它们 `pip install` 进这个 Python（site-packages 不可写时 pip 自动装到用户目录）；没变化时什么都不做。安装记录在 `data/lark-agent-requirements.sha256`。
- **数据**：技能自己的数据放在 `data/user/<名>/<技能名>/`（例如用户的登录信息和中间文件），不在 `workspace/` 里，所以不会被当成交付文件发给用户，也只由技能自己清理（见“自动清理”）。不属于哪个用户的放在 `data/shared/<技能名>/`，不清理、不发送。用户之间的隔离只是约定：智能体的命令没有限制，同一个 Windows 账号，被提示词注入的模型能读到别人的目录。
- **自我清理（`prune`）**：技能可以在 `SKILL.md` 开头的 `metadata` 里写 `prune: "scripts/<脚本>.py prune"`（技能目录里的 Python 脚本和参数）。自动清理时，服务对每个有 `<技能名>/` 文件夹、当时没有任务的用户目录运行一次（工作目录是技能目录，环境是这个用户的，没有会话），命令输出一行 `{"status":"ok","files":n,"bytes":n}`；每次最多 30 分钟，超时或服务停止时连同它启动的进程一起结束。删什么由技能自己决定，服务不知道技能文件夹里有什么。三个技能都有：feishu 清保存中断留下的临时文件和隐藏浏览器的缓存，audit-auto 清 `tmp/` 和留存的审核结果 `reviews/`，requirement-auto 清过期的批次和临时文件（见下表）。`start.cmd doctor` 的 `skills` 标出声明了 `prune` 的技能。
- **定时任务**：不属于哪个用户的一轮、要定期做的事，由技能在使用过程中写进它在 `data/shared/` 的文件夹：`data/shared/<技能名>/jobs.json`。服务启动时读一遍，之后每次例行检查（每小时）再读一遍，新出现的任务排上、删掉或改了的不再按原来的安排跑（正在跑的那一次让它跑完）。现在只有 audit-auto：有审核群在监听时，它写下每三天读一遍这些群的任务（每次最多读 30 分钟，到 30 分钟强制结束；结束时正在进行的那一段下次重读）（最后一个群停止监听时删掉）：

  ```json
  {"jobs":[{"name":"audit-rules-sync","everyMinutes":4320,"timeoutMinutes":30,"command":"python","args":["scripts/audit.py","rules","sync"]}]}
  ```

  排上后约 2 分钟跑第一次，之后每 `everyMinutes`（至少 5）分钟一次；在技能自己的文件夹 `skills/<技能名>/` 里运行（`python` 是系统的 Python，`node` 是运行本服务的 node），`skills/` 里没有这个技能时不运行。上一次还没跑完就跳过这一次，超过 `timeoutMinutes`（默认 30）就结束它；服务停止时正在跑的一并结束。环境同上，但没有用户目录、Open ID 和会话。输出的末尾写日志（`定时任务完成` / `定时任务失败`），不发给任何人。`/health` 的 `jobs` 是每个任务的状态，`start.cmd doctor` 列出各技能的定时任务和写错的地方。

  从项目根目录 `jobs.json` 升级过来、已经有审核群在监听的部署：`data/shared/audit-auto/jobs.json` 要等 audit-auto 下一次保存监听的群时才会写出来；升级后手动跑一次 `python skills\audit-auto\scripts\audit.py rules sync`（或在任一审核群里说「现在更新一下规范」）即可。
- **只放你信任的技能**：技能的脚本以本服务的账号、不受任何限制地运行。
- **机器人密钥的代价**：每个用户的每一轮命令都带着 `FEISHU_APP_SECRET`（feishu 技能的设备授权、audit-auto 读审核群都要用）。能和机器人对话的人理论上能让智能体读到它，和 `.env`、`data/pi/auth.json` 一样（见“访问控制”）。想收紧，给设备授权单独建一个飞书应用，把 `.env` 里的 `FEISHU_APP_ID/SECRET` 换成它。

## 自动清理

服务启动时（接收任务之前）先清理一次，之后每 72 小时一次：**超过 `RETENTION_DAYS`（`.env`，默认 14）天没有被动过**的下列内容会被删除。文件的“时间”取创建、修改、移动三者中最近的一个，所以几天前创建、但昨天还在用的文件不会被删。一时删不掉的（正被程序占用、没有权限）留到下一轮（72 小时后）。

| 范围 | 删除 | 不动 |
|---|---|---|
| `data/logs` | 日志文件（`.jsonl`） | 数据库、控制令牌 |
| `data/backup-*.sqlite` | `start.cmd backup` 生成的备份中，最新 3 份以外的 | 最新 3 份（不论多旧） |
| `data/user/<用户名>/workspace` | **里面所有文件**（用户发来的 `input/` 附件、智能体生成的文件，包括以 `.` 开头的），以及因此变空的文件夹 | 工作目录文件夹本身 |
| `data/user/<用户名>/tmp` | 智能体的中间文件 | 文件夹本身 |
| `data/user/<用户名>/<技能名>` | 由技能自己的 `prune` 决定（见“技能”）：feishu 是保存中断留下的 `*.tmp`（`FEISHU_RETENTION_DAYS`，默认 14 天）和隐藏浏览器的缓存（`Cache`、`GPUCache`、`Service Worker/CacheStorage` 等，**整个删，不看新旧**，浏览器在用这个配置时跳过）；audit-auto 是 `tmp/` 和 `reviews/`（`AUDIT_AUTO_RETENTION_DAYS`，默认 14 天）；requirement-auto 是批次 `batches/` 和 `tmp/`（`REQUIREMENT_AUTO_RETENTION_DAYS`，默认 7 天；一个批次整个删、里面有新文件就整个留）。用户不再用这个技能也照样清 | 飞书登录和浏览器里的飞书网页登录、audit-auto 的群设置、requirement-auto 的 CLS 登录 `cls/` 等技能自己的数据；`data/shared/` 整个不动 |
| `data/pi` | 对话文件 `sessions/*.jsonl`（每一轮都会写，用着的不会旧），崩溃记录 `crashes.json` | 登录 `auth.json` 等其余一切 |
| 数据库 | 结束超过 2×`RETENTION_DAYS` 天的任务记录（事件、卡片、附件、交付、投递），没有任务引用的已退役对话，以及最新 500 条以外已发出的提示；空闲页多时压缩文件 | 未结束的任务、还有投递未发出的任务、当前对话、用户目录分配 |

- 有任务排队或执行中的用户，这一轮不清理其用户目录（每个技能的 `prune` 开始前再看一次）；只要有任务在执行，这一轮也不清理 `data/pi`。
- 链接（符号链接、目录联接）只删链接本身，从不进入它指向的位置；本身是链接的工作目录整个跳过。
- 用户很久没来，再发消息时机器人仍按原对话号续接；找不到时自动新开对话（见“会话”）。工作目录里的旧文件已经没了，需要的要让智能体重新生成。
- 清掉的文件如果还有没送达的投递，该投递标记为失败，不再重试。
- 每个任务自己接收的附件和交付过的文件，在任务结束、投递完成并过了保留期后也会按任务清理：只删任务记录里登记过的文件，且必须在该用户的工作目录内（附件只认 `input/` 里的），不满足就跳过并写日志，不会误删。任务记录随后按上表从数据库中清除，之后 `retry-delivery` 也不能再重发它们。
- 有文件被删时日志记一条“已清理超过保留期的数据”（各类和各技能的文件数和字节数），有记录被清除时记一条“已清理过期的任务记录”；技能的 `prune` 失败时记“技能的过期数据清理失败”。

## 访问控制

`.env` 中：`ALLOWED_TENANTS`（整个企业开放，优先于用户名单，管理员也受限）→ 否则 `ALLOWED_USERS` / `ADMIN_USERS`（Open ID）。三者皆空则拒绝所有任务。`ALLOWED_CHATS` 可额外限制会话。改后重启。

机器人不提供按任务查询或取消的命令，用户只能操作自己的对话。所有任务共享本机权限，没有任何限制，用户之间也没有隔离：智能体能读写其他用户的工作目录，也能读到 `.env`（含 AI 网关 key）和 `data/pi/auth.json`（共用的 ChatGPT 登录）。只对可信内部用户开放。

## 指令

机器人自己只认一个：**`/new`**，忘掉之前的对话，下一条消息开始新对话；如果有任务正在执行或排队，先结束它。必须是单独的 `/new`（不带附件、不带别的文字）。其他以 `/` 开头的消息，包括 `/help`、`/status`，都当作普通文字交给智能体，机器人不会拦截。没有获得授权的人发任何消息，机器人都会回复他的 Open ID 和租户，管理员据此填入 `.env`。

## 运维

```bat
start.cmd doctor
start.cmd login [device]        :: 用 ChatGPT 订阅登录；logout 退出
start.cmd setup                 :: 把技能的 Python 依赖装进系统 Python（start 时也会自动做）
start.cmd stop                  :: 通过本机令牌接口安全停止，也可在服务窗口按 Ctrl+C
start.cmd backup                :: 备份数据库到 data/
start.cmd retry-delivery <任务ID|all>  :: 重试失败的投递，不重跑任务
```

健康检查 `http://127.0.0.1:8765/health`（`agent` 为智能体是否可用、pi 的版本和模型；`skills` 为技能；`jobs` 为定时任务的状态；`feishu.state` 为长连接实时状态：connecting、connected、reconnecting、failed），就绪 `/ready`（未就绪 503）。飞书长连接跟着例行检查每小时看一次：还没建立就建立；没连通算一次失败，之后改为每 30 秒重试一次，直到连通为止；连续 3 次失败，或 SDK 已经放弃（比如凭证被拒），就关掉整个重建，重建后照样每 30 秒重试。连通后回到每小时看一次。每次断开、连通都写日志。两次例行检查之间断开的，要到下一次例行检查才会被发现（SDK 自己的重连仍然随时进行）。SDK 自己也会重连：建连 20 秒没完成就重试，发出心跳 30 秒没有回应就断开重连。

**例行检查**：服务启动时一次，之后每小时一次，一起做这几件事：看飞书长连接（见上）、检查 ChatGPT 登录（没就绪时另外每 10 秒查一次，所以登录后 10 秒内生效，不用重启）、重新读各技能的定时任务、校对本机时钟和飞书时间、在日志里写一条“服务运行中”。日志在 `data/logs`，每天每个进程一个文件，保留期见“自动清理”；`.env` 可选 `LOG_LEVEL`。

**进程**：`start.cmd` 在当前窗口里直接运行服务，没有守护进程：服务退出或卡住都不会自动重启，要重新运行 `start.cmd`；重启后，上次没做完的任务按启动时的规则作废。健康检查端口已被占用（已有一份服务在运行）时启动失败。`/health` 的 `pollAgeMs` 是上一次例行检查多久前跑完的，超过一小时还在变大说明服务卡住了；每次例行检查都在日志里写一条“服务运行中”，日志里超过一小时什么都没有，也说明进程停住了。

**控制台**：只显示几类信息，每条一行，带本机时间：

```
[09:30:12] 等待：还没有登录 ChatGPT（openai-codex），请运行 start.cmd login
[09:30:41] 启动成功，技能：audit-auto、requirement-auto
[09:31:05] 李小明 提交任务：帮我审一下这条视频（附件 1 个）
[09:32:40] 李小明（群聊） 提交任务：监听本群更新审片规范
```

- “启动成功”在智能体就绪、飞书长连接连通时出现一次（没有配置开放的企业或用户时会注明任务不会执行）。在此之前，服务在等什么（ChatGPT 没登录、飞书配置或连接有问题）每种原因显示一次；启动时需要安装 Python 包、启动失败时也各显示一行。
- 用户每发一条消息显示一行：姓名取自飞书（同一个用户 6 小时内只查一次；查不到时用这个用户上次的显示名，再没有就显示 Open ID）、群聊会注明，内容只显示第一行的前 60 个字，附件只显示个数。执行中并入的消息也算一条；飞书重复推送的同一条消息只显示一次。
- 其余一切（任务执行、回传、长连接断开重连、错误等）只写日志文件 `data/logs`。控制台由单独的线程写：窗口停住（比如在窗口里选中了文字）不会卡住服务。

## 版本

`@earendil-works/pi-coding-agent` 和 `@earendil-works/pi-ai` 固定同一版本（现为 1.5.2），升级时两个一起改。任务通过 SDK 的 `createAgentSession` 执行（`prompt` 开始一轮，执行期间的消息用 `steer`，`/new` 用 `abort`），`src/agent/runner.ts` 把会话事件换成本项目的步骤（`src/agent/turn.ts`），卡片由这些步骤生成。升级后看一下 pi 的 CHANGELOG 里 SDK、会话事件和 `openai-codex` 登录有没有变化，再跑 `npm test`、`start.cmd doctor`。各技能的测试（feishu、audit-auto、requirement-auto）：`python -m unittest discover -s skills\<名>\tests -t skills\<名>`。

数据库（`data/agent.sqlite`）的版本记在 `user_version` 里，现为 3：版本 2 加了任务状态 `STEERING`/`STEERED`，版本 3 去掉了 Codex 生成图片的表（`task_generated_images`）；版本 1、2 的库启动时直接沿用并改记为 3（删掉那张表），其他版本的库会被拒绝，启动时报错并说明是哪个文件。所以升级后不能直接回滚到 Codex 版（它会拒绝这个库）；确需回滚，先停服务，用任意 SQLite 工具执行 `CREATE TABLE task_generated_images (task_id TEXT NOT NULL REFERENCES tasks(id),item_id TEXT NOT NULL,saved_path TEXT,PRIMARY KEY(task_id,item_id)); PRAGMA user_version=2;` 再启动旧版。表结构变化时要同时升版本并写明处理办法。

单元测试不能替代实机验收：改动后请用飞书私聊和群聊验证卡片、附件回传，以及重启后继续同一个对话。

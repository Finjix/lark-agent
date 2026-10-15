# 飞书应用配置

接入方式：企业自建应用、机器人、官方 SDK 出站 WebSocket；不需要公网回调地址。

## 最小应用权限

```json
{
  "scopes": {
    "tenant": [
      "im:message:send_as_bot",
      "im:message.p2p_msg:readonly",
      "im:message.group_at_msg:readonly",
      "im:resource",
      "im:message:readonly",
      "cardkit:card:write"
    ],
    "user": []
  }
}
```

`im:message:readonly` 是获取用户消息中附件的下载接口可接受的只读消息权限，只有 `im:resource` 仍会被拒绝。接口报错也可能列出旧权限 `im:message.history:readonly`，但当前后台已无法新增申请该旧权限。机器人仍只处理订阅收到的消息，不主动扫描聊天历史。应用可用范围按实际内部用户设置；服务可通过 `ALLOWED_TENANTS` 对指定企业全员开放，或通过 `ALLOWED_USERS` 逐人开放。

## 事件与发布

1. 启用机器人能力。
2. 本地配置凭证并启动服务。
3. “事件与回调”选择“使用长连接接收事件”，验证成功后保存。
4. 添加“接收消息”事件 `im.message.receive_v1`，使用应用身份订阅。
5. 创建应用版本，检查六项权限、事件和可用人员范围后发布；需要企业管理员审批时由管理员完成。
6. 私聊发送 `/whoami`，把租户标识填入 `ALLOWED_TENANTS` 即可对同企业成员开放；若只开放个人，则留空企业名单并将 Open ID 填入 `ALLOWED_USERS`。停止并重新启动后发送一个测试任务。

群聊仅处理 @当前机器人消息，使用机器人 Open ID 精确判断；不订阅全群历史消息。

## 常见问题

- 长连接已启动但收不到任务：检查消息事件、应用是否发布、可用范围和群内是否 @正确机器人。
- 文本可发送但文件失败：检查 `im:resource` 是否仍处于“待发布”。
- 用户附件下载失败并提示权限：开通应用身份权限 `im:message:readonly`，发布新版并完成审批。
- 发送图片任务时，可将图片和处理要求放在同一条图文消息中；支持单独图片和文件消息。入站附件每条最多 10 个、单个最多 30 MB，下载失败会在回复中说明原因。
- `/ready` 返回 503：检查凭证、白名单、Desktop 是否运行，以及 `doctor.cmd` 输出。
- 发出任务后还没有回复：可用 `/tasks`、`/status 任务ID` 检查 Desktop 连接、同会话前序消息、`UNKNOWN` 任务或共享资源冲突；任务调度不设并发数量上限。
- 流式回复使用无标题的 CardKit 消息，只显示公开过程和回复正文。超过九分钟后，服务自动切换为完整内容更新，继续展示后续输出。

参考：[官方 Node SDK](https://github.com/larksuite/node-sdk)、[CardKit 流式文本接口](https://open.feishu.cn/document/cardkit-v1/card-element/content)。

## 后续功能按需申请的权限

### 会话自动命名

按发送者显示名命名会话时，开通并发布应用身份权限 `contact:contact.base:readonly`（获取通讯录基本信息）及 `contact:user.base:readonly`（获取用户基本信息），并配置对应用户的可见范围。前者允许访问用户查询接口，但缺少后者时接口可能成功却不返回姓名字段。服务只查询已有会话的发送者，保留显示名，不扫描整个通讯录；查询失败不会阻断任务。

基本任务收发只需上面的消息、附件和卡片权限，按显示名命名额外使用上述用户基本信息权限。直接收发 Word、Excel、PDF、ZIP 等文件不需要对应的飞书云文档权限；处理飞书在线链接是另一类能力，开通权限后还需实现对应功能，并取得目标资源访问权限。

| 计划功能 | 候选权限 | 建议 |
|---|---|---|
| 读取用户提供的飞书文档链接 | `docx:document:readonly` | 文档分析功能实现时申请，只读起步 |
| 读取知识库节点及其中的文档 | `wiki:wiki:readonly`、`docx:document:readonly` | 需要对应知识空间和文档的协作授权 |
| 读取在线电子表格 | `sheets:spreadsheet:readonly` | 与上传的 `.xlsx` 文件区分 |
| 查询多维表格 | `bitable:app:readonly` | 后续写回数据时，再按使用的 API 申请相应写权限 |

暂不为预想功能批量开通群聊全量消息、通讯录、日历、审批或云盘管理权限。未来需要创建在线文档、修改表格、上传云盘、处理日程时，按具体 API 补充最小权限及资源授权。

参考：[官方文档与知识库实践](https://open.feishu.cn/community/articles/7293415563448238084)、[官方电子表格实践](https://open.feishu.cn/community/articles/7302287257225445404)、[多维表格权限说明](https://open.feishu.cn/document/base-extensions/base-table-view-extension-development-guide)。

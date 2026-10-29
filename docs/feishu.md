# 飞书应用配置

接入方式：企业自建应用、机器人、官方 SDK 出站长连接，不需要公网回调地址。

## 应用权限（应用身份）

```json
{
  "scopes": {
    "tenant": [
      "im:message:send_as_bot",
      "im:message.p2p_msg:readonly",
      "im:message.group_at_msg:readonly",
      "im:resource",
      "im:message:readonly",
      "cardkit:card:write",
      "contact:contact.base:readonly",
      "contact:user.base:readonly"
    ],
    "user": []
  }
}
```

- 发消息和卡片：`im:message:send_as_bot`、`cardkit:card:write`
- 收私聊/群 @ 消息：`im:message.p2p_msg:readonly`、`im:message.group_at_msg:readonly`
- 上传交付文件和图片：`im:resource`
- 下载用户附件：`im:message:readonly`（只有 `im:resource` 会被拒绝）
- 查用户显示名（用来给 `user/` 下该用户的目录命名）：`contact:contact.base:readonly`、`contact:user.base:readonly`（缺后者时接口成功但无姓名）

机器人只处理订阅收到的消息，不扫描聊天历史或通讯录。应用可用范围需包含目标成员；缺少姓名时任务照常执行，用户目录改用 Open ID 命名。

## 插件需要的用户身份权限

机器人自己只用上面的应用身份权限。放进 `plugins/` 的插件要替用户操作飞书时，都经由 feishu 插件：用户用设备授权登录（feishu 插件在这个用户的无头浏览器里打开授权页，用户用飞书手机端扫一次码；令牌存在 `user/<名>/feishu/`），各插件共用这一个登录。这时应用的“用户身份”权限要包含插件需要的（以 feishu 插件 README 的清单为准，现为 `wiki:wiki:readonly sheets:spreadsheet:readonly sheets:spreadsheet:read sheets:spreadsheet:write_only sheets:spreadsheet drive:export:readonly docx:document:readonly docx:document offline_access`），发布新版本后用户首次授权会看到这些权限。另外要在应用里允许用户身份授权（设备授权）。授权时报“应用凭证无效”（`invalid_client`），先查 `.env` 里的应用凭证，再查这一项。插件用的就是机器人的应用（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`），代价见 README“插件”一节。

## 事件与发布

1. 启用机器人能力。
2. 在 `.env` 配置凭证并启动服务。
3. “事件与回调”选择“使用长连接接收事件”，验证成功后保存。
4. 添加“接收消息”事件 `im.message.receive_v1`，使用应用身份订阅。
5. 创建应用版本，检查权限、事件和可用范围后发布；需要审批时由企业管理员完成。
6. 私聊随便发一条消息：未授权时机器人会回复你的租户标识和 Open ID，填入 `ALLOWED_TENANTS` 对整个企业开放，或留空并把 Open ID 填入 `ALLOWED_USERS` 只开放个人。重启服务后发送测试任务。

群聊只处理 @当前机器人的消息。

## 排错

- 收不到任务：查事件订阅、应用是否发布、可用范围、群里是否 @对机器人。
- 文件发送失败：`im:resource` 未发布。附件下载失败：`im:message:readonly` 未发布。
- `/ready` 503：检查凭证、白名单、`.codex/config.toml` 里的 AI 网关配置，看 `start.cmd doctor`。
- 无回复：同一用户的上一条输入未结束时，新输入不会被接收；发 `/new` 可以结束它并重新开始。任务状态看 `/health` 和 `data/logs`。

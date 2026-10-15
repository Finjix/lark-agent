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
      "cardkit:card:write"
    ],
    "user": []
  }
}
```

不申请通讯录、云盘或文档读取权限。应用可用范围按实际内部用户设置，服务自己的白名单仍需配置。

## 事件与发布

1. 启用机器人能力。
2. 本地配置凭证并启动服务。
3. “事件与回调”选择“使用长连接接收事件”，验证成功后保存。
4. 添加“接收消息”事件 `im.message.receive_v1`，使用应用身份订阅。
5. 创建应用版本，检查五项权限、事件和可用人员范围后发布；需要企业管理员审批时由管理员完成。
6. 私聊发送 `/whoami`，把本人的 Open ID 填入 `ALLOWED_USERS`；停止并重新启动后发送一个测试任务。

群聊仅处理 @当前机器人消息，使用机器人 Open ID 精确判断；不订阅全群历史消息。

## 常见问题

- 长连接已启动但收不到任务：检查消息事件、应用是否发布、可用范围和群内是否 @正确机器人。
- 文本可发送但文件失败：检查 `im:resource` 是否仍处于“待发布”。
- `/ready` 返回 503：检查凭证、白名单、Desktop 是否运行，以及 `doctor.cmd` 输出。
- 卡片只停留在排队：核对 Desktop 版本、`UNKNOWN` 任务占用和用户配额。
- 超过十分钟的任务：服务自动将流式卡片切换为普通完整卡片更新，继续展示进度。

参考：[官方 Node SDK](https://github.com/larksuite/node-sdk)、[CardKit 流式文本接口](https://open.feishu.cn/document/cardkit-v1/card-element/content)。

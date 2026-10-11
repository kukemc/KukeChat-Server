# 群等级与等级头衔

## 升级曲线

从 L 级升到 L+1 级需要 `40 + 12 × (L - 1)` 经验，最高 100 级。

| 等级 | 累计经验 |
| --- | --- |
| 10 | 792 |
| 20 | 2 812 |
| 50 | 16 072 |
| 100 | 62 172 |

经验来源：

- 发言：文本每条 +2，图片 / 语音每条 +3，每人每群每天最多 100
- 签到：基础 30；连签 3 / 7 / 30 / 100 天分别再 +10 / +25 / +80 / +200；当天全群第一个签到再 +15

活跃成员一天大约 130–160 经验：10 级一周内，20 级约三周，50 级约四个月，100 级一年多。

曲线和常量在 `app/core/group_levels.py` 与 `app/services/group_activity.py`。`conversation_members.level` 是按 `level_exp` 算出来的缓存，改曲线后要回填一次。

## 等级头衔

按等级区间显示的称号（1–10 级「萌新」之类）。每个群自己决定开不开、怎么分档，默认关闭。

会话对象（`GET /conversations`、会话详情、`conversation.updated` 事件）上有两个字段：

```jsonc
{
  "level_titles_enabled": false,
  "level_titles": [                      // 始终是生效的分档，按 min_level 升序；没自定义时是默认分档
    { "min_level": 1,  "title": "萌新" },
    { "min_level": 11, "title": "活跃" },
    { "min_level": 21, "title": "熟面孔" },
    { "min_level": 31, "title": "骨干" },
    { "min_level": 46, "title": "元老" },
    { "min_level": 61, "title": "传说" },
    { "min_level": 81, "title": "殿堂" }
  ]
}
```

某个成员的等级头衔 = `min_level` 不超过其等级的最后一档。消息上不单独下发头衔，客户端用 `sender_level` 自己算。

修改走现有的群设置接口 `PATCH /conversations/{id}/settings`（群主 / 管理员）：

```jsonc
{ "level_titles_enabled": true,
  "level_titles": [{ "min_level": 1, "title": "新人" }, { "min_level": 21, "title": "大佬" }] }
```

- 两个字段都可选，可以只传其中一个。
- `level_titles` 是整体替换；传 `[]` 恢复默认分档。
- 约束：最多 12 档；`min_level` 在 1–100 之间且不重复；必须有一档从 1 级开始；`title` 去掉首尾空格后 1–8 个字。服务端会按 `min_level` 排序后保存。
- 校验失败返回 422，`detail = { "code": "invalid_level_titles", "message": "<可直接展示的中文>" }`。
- 开启内容审核时头衔会过文本审核，被拦截返回 422，`detail.code = "level_titles_rejected"`。
- 非群主 / 管理员返回 403。

修改成功后群成员会收到现有的 `conversation.updated`，没有新的实时事件。

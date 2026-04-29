# `secrets/` — 凭据本地副本

这个目录**永远不进 git**（已在仓库根 `.gitignore` 里登记 `/secrets/`）。它存的是各种凭据的
**本地副本**，方便你离线查阅、追踪谁拿了哪把 key、日后 rotate 时知道作废哪些。

> ⚠️ **凭据真正的"权威源"**不在这里。Clove 的 API key / admin key 实际生效是写在
> `~/.clove/data/config.json`；OAuth token 在 `~/.clove/data/accounts.json`。**这里的文件只是
> 你给自己看的笔记**——真要改 key，得通过 admin API 调 `PUT /api/admin/settings`，那边写完
> Clove 才认。
>
> 即便如此，这些笔记仍然是敏感数据：泄漏一把 user key = 别人能用你的 Pro/Max 订阅；泄漏 admin
> key = 别人能改你 Clove 配置 / 看 OAuth token。

## 目录结构

```
secrets/
├── README.md         # 本文件，可入 git（不含敏感）
└── api-keys.md       # 全部 user/admin key 清单（gitignored）
```

将来如果有别的凭据类型（OAuth token 备份、SSH 私钥等），也放这里、加进 `.gitignore`。

## 怎么读 `api-keys.md`

每行一把 key，标注**给谁用了 / 何时签发 / 状态**。日常你只要做三件事：

1. **新加一把**（多一个朋友）：
   - 先写到 `api-keys.md` 文档里占位
   - 跑下面 §"加 key" 的命令
   - 把 key 发给朋友（用安全渠道：1Password / Bitwarden / Signal，**别**用普通 IM）
2. **吊销一把**（朋友不用了 / 怀疑泄漏）：
   - 在 `api-keys.md` 把它标 `status: revoked` + 写日期
   - 跑下面 §"吊销 key" 的命令
   - 通知朋友 key 失效了
3. **季度性审查**：每 3 个月扫一遍，长期 unused 的标记或吊销

---

## 加一把 key

```bash
ADMIN_KEY='你的 admin key（从 api-keys.md 拷）'
NEW_KEY="sk-clove-<owner>-$(uv run --directory /home/winbeau/clove python -c \
  'import secrets; print(secrets.token_urlsafe(24))')"
echo "$NEW_KEY"   # ← 记下来，写进 api-keys.md

# 拿现有数组
EXISTING=$(curl -sS -H "x-api-key: $ADMIN_KEY" \
  http://127.0.0.1:5201/api/admin/settings | jq -c '.api_keys')

# 追加并 PUT
NEW_LIST=$(echo "$EXISTING" | jq --arg k "$NEW_KEY" '. + [$k]')
curl -sS -X PUT -H "x-api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d "{\"api_keys\": $NEW_LIST}" http://127.0.0.1:5201/api/admin/settings | jq '.api_keys'
```

**命名约定**：`sk-clove-<owner>-<random24>`，`<owner>` 用人名/朋友昵称，便于审计时一眼看清。

## 吊销一把 key

```bash
ADMIN_KEY='...'
KEY_TO_REMOVE='sk-clove-friend2-jXz...'

EXISTING=$(curl -sS -H "x-api-key: $ADMIN_KEY" \
  http://127.0.0.1:5201/api/admin/settings | jq -c '.api_keys')

NEW_LIST=$(echo "$EXISTING" | jq --arg k "$KEY_TO_REMOVE" '. - [$k]')
curl -sS -X PUT -H "x-api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d "{\"api_keys\": $NEW_LIST}" http://127.0.0.1:5201/api/admin/settings | jq '.api_keys'
```

## 轮换 admin key（建议每季度一次）

```bash
OLD_ADMIN='当前用的 admin key'
NEW_ADMIN="sk-admin-clove-$(uv run --directory /home/winbeau/clove python -c \
  'import secrets; print(secrets.token_urlsafe(24))')"
echo "$NEW_ADMIN"   # ← 写进 api-keys.md

# 第一步：先把新旧两把都加到 admin_api_keys 里
curl -sS -X PUT -H "x-api-key: $OLD_ADMIN" -H 'content-type: application/json' \
  -d "{\"admin_api_keys\": [\"$OLD_ADMIN\", \"$NEW_ADMIN\"]}" \
  http://127.0.0.1:5201/api/admin/settings | jq '.admin_api_keys'

# 第二步：用新 admin key 验证能调
curl -sS -H "x-api-key: $NEW_ADMIN" http://127.0.0.1:5201/api/admin/settings | head -c 60

# 第三步：移除老 admin key
curl -sS -X PUT -H "x-api-key: $NEW_ADMIN" -H 'content-type: application/json' \
  -d "{\"admin_api_keys\": [\"$NEW_ADMIN\"]}" \
  http://127.0.0.1:5201/api/admin/settings | jq '.admin_api_keys'
```

中间那一步同时持有新旧两把是为了避免"新 key 写错了，老的也用不了"——回滚时还能拿老的救场。

---

## 客户端怎么用 key

朋友拿到 key 后，在他自己电脑上：

```bash
mkdir -p ~/.claude
cat > ~/.claude/settings.json <<EOF
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://claude.selab.top:8443",
    "ANTHROPIC_AUTH_TOKEN": "<他那把 sk-clove-… key>"
  }
}
EOF
claude   # Claude Code
```

或者临时用环境变量：

```bash
export ANTHROPIC_BASE_URL='https://claude.selab.top:8443'
export ANTHROPIC_AUTH_TOKEN='<他那把 key>'
unset ANTHROPIC_API_KEY    # 防 collision
claude
```

非 Claude Code 的客户端（SillyTavern / Cline / 自写 Anthropic SDK 脚本）思路一样：base URL +
token 设上即可。

---

## 安全建议

1. **传递 key 用安全渠道**：1Password share / Bitwarden Send / Signal 私聊。**不要**走微信、
   邮件、Slack DM。
2. **每把 key 只给一个人**：审计时能定位到具体人；一人泄漏吊销不影响其他人。
3. **VPS 上的 `secrets/` 目录**：本身已经 gitignored，但**别在 share 屏幕、录屏教学时**打开它。
4. **客户端机器上**：朋友的 `~/.claude/settings.json` 也是机器内的明文——他要是用工作电脑，记
   得日后离职/换电脑时清掉。
5. **怀疑 key 泄漏**：立刻吊销，签发新 key，不要"等等看"。配额一旦被耗光会影响所有人。

---

## 相关文档

- 主部署文档：[../docs/deployment.md](../docs/deployment.md)
- 日常运维手册：[../docs/notes/04-operations-runbook.md](../docs/notes/04-operations-runbook.md)

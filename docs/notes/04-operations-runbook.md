# 04 — 日常运维手册

> **遇到什么 → 看这里**：
> - 服务挂了怎么看日志
> - 改了配置 / 代码怎么生效
> - 想加/换 API key
> - 朋友的 Claude Code 报 401 / 配额耗尽
> - 月底 OAuth token 要过期怎么办
> - 升级到 upstream Clove 新版本

跟前三篇"故事 + 原理"风格不同，这篇是** cheat sheet 式**的——遇到具体问题直接 grep 这一篇。

---

## 目录

1. [日常监控：日志在哪](#1-日常监控日志在哪)
2. [重启与重新加载](#2-重启与重新加载)
3. [API key 管理](#3-api-key-管理)
4. [Claude OAuth 账号管理](#4-claude-oauth-账号管理)
5. [TLS 证书续签](#5-tls-证书续签)
6. [配额触发 / RATE_LIMITED 处理](#6-配额触发--rate_limited-处理)
7. [升级 Clove 主线版本](#7-升级-clove-主线版本)
8. [关键文件位置 cheat sheet](#8-关键文件位置-cheat-sheet)

---

## 1. 日常监控：日志在哪

### Clove 日志

systemd 抓 stderr 进 journalctl：

```bash
# 实时跟流
sudo journalctl -u clove -f

# 最近 100 行
sudo journalctl -u clove -n 100 --no-pager

# 时间范围
sudo journalctl -u clove --since "1 hour ago"
sudo journalctl -u clove --since "2026-04-29 03:00" --until "2026-04-29 04:00"

# 只看 ERROR
sudo journalctl -u clove --no-pager | grep -i error
```

Clove 还会写文件日志（`.env` 里 `LOG_TO_FILE=true`）：

```bash
tail -f ~/.clove/data/logs/app.log
```

journalctl 跟文件日志内容一样，文件日志按 `LOG_FILE_ROTATION` 自动轮转（默认 10MB / 14 天）。

### Caddy 日志

```bash
sudo journalctl -u caddy -f       # 包括请求访问日志 + ACME 证书相关
```

Caddy 写 JSON 格式日志，看 ACME 续签时关键字搜：

```bash
sudo journalctl -u caddy --since "1 day ago" | grep -iE 'cert|acme|obtain|renew'
```

### 关注什么信号

| 信号 | 含义 |
|---|---|
| `account: ... is RATE_LIMITED` | 账号触发了配额限制 |
| `OAuth refresh failed` | refresh token 失效，账号需要重新 bootstrap |
| `Successfully processed request via Claude API` | OAuth path 正常，预期看到 |
| `Claude.ai 网页注入` 字样 | 路由到 web 反代，**不预期**——检查 `auth_type` |
| `503 Service Unavailable` | 上游 Anthropic 临时故障，等几分钟 |

---

## 2. 重启与重新加载

### 改了 Clove 代码（拉了新提交、改了 .env）

```bash
cd /home/winbeau/clove
git pull
uv sync --extra rnet      # 仅当依赖变化才需要
sudo systemctl restart clove

# 验证
sudo journalctl -u clove -n 20
curl -sS -H "x-api-key: $ADMIN_KEY" http://127.0.0.1:5201/api/admin/accounts | jq
```

### 改了 Caddyfile

```bash
sudo install -m 0644 /home/winbeau/clove/deploy/Caddyfile /etc/caddy/Caddyfile

# 优先用 reload（不丢 TLS 连接）
sudo systemctl reload caddy

# 如果 reload 不工作，退化到 restart
sudo systemctl restart caddy
```

reload 与 restart 区别：

| 操作 | 行为 | 何时用 |
|---|---|---|
| `reload` | 平滑加载新配置；现有连接继续用旧配置直到关闭 | 改 Caddyfile 内容 |
| `restart` | 关掉所有连接重启进程 | 改了 unit 文件、reload 不工作 |

### 改了 admin UI 里的设置（API key、超时、模型映射等）

**不需要重启**。Clove 把改动写到 `~/.clove/data/config.json` 后立刻生效（在内存里）。

### 改了 .env

`.env` 是 systemd 在启动时读的（通过 `EnvironmentFile=`），需要：

```bash
sudo systemctl restart clove
```

### 改了 systemd unit 自身（clove.service / caddy.service）

```bash
sudo install -m 0644 /home/winbeau/clove/deploy/clove.service /etc/systemd/system/clove.service
sudo systemctl daemon-reload     # 必须，让 systemd 重读 unit
sudo systemctl restart clove
```

`daemon-reload` 是关键——不重读 unit，systemd 还按旧配置跑。

---

## 3. API key 管理

Clove 的 API key 体系：

- **admin keys**（`config.json` 里的 `admin_api_keys`）：能调 `/api/admin/*`，包括看账号、改设置
- **user keys**（`api_keys`）：只能调 `/v1/*` 推理端点

两类都是普通字符串，没强制格式。我们约定的命名前缀：

- `sk-admin-clove-*` — admin key
- `sk-clove-self-*` — 你自己用
- `sk-clove-friend-*` — 朋友用

### 列出当前 keys

```bash
ADMIN_KEY='你的 admin key'
curl -sS -H "x-api-key: $ADMIN_KEY" http://127.0.0.1:5201/api/admin/settings | \
  jq '{api_keys, admin_api_keys}'
```

### 加一把新 user key（给第二个朋友用）

PUT settings 时要给**完整的新数组**——它是覆盖式的，不是增量。

```bash
ADMIN_KEY='你的 admin key'

# 先生成一把强随机 key
NEW_KEY="sk-clove-friend2-$(uv run python -c 'import secrets; print(secrets.token_urlsafe(24))')"
echo "$NEW_KEY"   # 记下来

# 拿现有 keys（避免覆盖时丢掉旧的）
EXISTING=$(curl -sS -H "x-api-key: $ADMIN_KEY" http://127.0.0.1:5201/api/admin/settings | \
           jq -c '.api_keys')

# 拼成新数组并 PUT
NEW_LIST=$(echo "$EXISTING" | jq --arg k "$NEW_KEY" '. + [$k]')
curl -sS -X PUT -H "x-api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d "{\"api_keys\": $NEW_LIST}" http://127.0.0.1:5201/api/admin/settings | jq '.api_keys'
```

写入 `~/.clove/data/config.json` 即时生效，不需要重启 Clove。

### 吊销一把 key（怀疑泄漏）

把 array 里那把删掉，PUT 新数组。同上 PUT 操作。

### 轮换 admin key（推荐定期，比如每季度）

1. 生成新 admin key
2. PUT settings 时同时把新旧 admin key 都包含
3. 用新 admin key 验证能调 admin API
4. 再 PUT 一次只保留新 admin key

中间步骤是为了避免"我把唯一的 admin key 改错了，现在我自己也进不去了"。

---

## 4. Claude OAuth 账号管理

### 看当前账号状态

```bash
ADMIN_KEY='你的 admin key'
curl -sS -H "x-api-key: $ADMIN_KEY" http://127.0.0.1:5201/api/admin/accounts | jq
```

关键字段：

| 字段 | 含义 |
|---|---|
| `status` | `valid` / `invalid` / `rate_limited` |
| `auth_type` | `oauth_only` / `cookie_only` / `both` |
| `is_max` | true 才能跑 max plan 限制的模型 |
| `has_oauth` | 必须 true |
| `last_used` | 最近一次成功调用时间 |
| `resets_at` | rate_limited 时显示什么时候配额恢复 |

### 当前账号过期 / refresh 失败

OAuth access token 短命（约 1 小时），但有 `refresh_token` 自动续；只要 refresh_token 有效，
token 续命对你完全透明。

但 **refresh_token 也有寿命**（具体长度未公开，业界普遍 30-90 天）。如果你看到日志：

```
ERROR | OAuth refresh failed
```

或账号状态变成 `invalid`，说明 refresh token 也失效了——账号需要重新 bootstrap。

### 重新 bootstrap：从笔记本再抓一遍

最快路径（同 sub-doc 01）：

1. 笔记本上 Claude Code 重新登录：

   ```bash
   claude logout
   claude login    # 浏览器走一次完整 OAuth
   ```

2. 重新读凭据：

   ```bash
   cat ~/.claude/.credentials.json | jq '.claudeAiOauth'
   ```

3. 在 VPS 上**先删旧账号**：

   ```bash
   ORG_UUID='你的 org UUID'
   ADMIN_KEY='你的 admin key'
   curl -sS -X DELETE -H "x-api-key: $ADMIN_KEY" \
     http://127.0.0.1:5201/api/admin/accounts/$ORG_UUID
   ```

4. 把新 token POST 进来（参考主文档 §5 步骤 4 的 BUNDLE 格式）

   ```bash
   BUNDLE='{
     "oauth_token": {
       "access_token": "<新 accessToken>",
       "refresh_token": "<新 refreshToken>",
       "expires_at": 1234567.890
     },
     "organization_uuid": "'$ORG_UUID'",
     "capabilities": ["claude_max", "chat"]
   }'
   curl -sS -X POST -H "x-api-key: $ADMIN_KEY" -H 'content-type: application/json' \
     -d "$BUNDLE" http://127.0.0.1:5201/api/admin/accounts
   ```

5. 验证：

   ```bash
   SELF_KEY='你的 user key'
   curl -sS -X POST -H "x-api-key: $SELF_KEY" -H 'anthropic-version: 2023-06-01' \
     -H 'content-type: application/json' \
     -d '{"model":"claude-sonnet-4-5","max_tokens":32,"messages":[{"role":"user","content":"ping"}]}' \
     https://claude.selab.top:8443/v1/messages
   ```

### 加第二个账号（多个 Pro/Max 订阅做负载均衡）

POST 多个账号时 Clove 会按 `last_used`、`status`、`is_max` 等做选择：

```bash
# 第二个账号的 BUNDLE
BUNDLE2='{
  "oauth_token": { ... },
  "organization_uuid": "<另一个 org UUID>",
  "capabilities": [...]
}'
curl -sS -X POST -H "x-api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d "$BUNDLE2" http://127.0.0.1:5201/api/admin/accounts
```

Clove 会自动在两个账号之间负载分配。如果一个达到 rate limit，自动切到另一个。

---

## 5. TLS 证书续签

### Caddy 自己管的（推荐设置）

Caddy 用 certmagic 内部续签：

- 默认证书剩余 30 天时开始尝试续签
- 第一次失败会退避重试，最多到证书真过期（90 天那天）
- 续签成功后**自动 reload**，不需要你做任何事

### 验证续签确实在跑

平时只要看日志确认：

```bash
sudo journalctl -u caddy --since "30 days ago" | grep -iE 'obtain|renew'
```

应该能看到（每次大约 60 天一次）：

```
"msg":"obtaining new certificate","identifier":"claude.selab.top"
"msg":"certificate obtained successfully","issuer":"acme-v02..."
```

### 手动强制续签（应急）

```bash
sudo caddy reload --config /etc/caddy/Caddyfile --force
```

或更激进：

```bash
sudo systemctl restart caddy
```

restart 时 Caddy 会重新检查所有证书，必要时立即续签。

### 证书续签失败的兜底（如果腾讯云 CAM 有问题）

可以临时用 acme.sh 兜底：

```bash
# 走 acme.sh（DNSPod 国际版）签个新证书
unset DP_Id DP_Key
 export DPI_Id='...'
 export DPI_Key='...'
~/.acme.sh/acme.sh --issue --dns dns_dpi -d claude.selab.top --force

# 装到 Caddy 看得到的位置
~/.acme.sh/acme.sh --install-cert -d claude.selab.top --ecc \
  --key-file       /home/winbeau/clove/deploy/certs/claude.selab.top.key \
  --fullchain-file /home/winbeau/clove/deploy/certs/claude.selab.top.crt
```

然后改 Caddyfile 改成静态 cert：

```caddy
claude.selab.top:8443 {
    tls /home/winbeau/clove/deploy/certs/claude.selab.top.crt \
        /home/winbeau/clove/deploy/certs/claude.selab.top.key
    ...
}
```

`systemctl reload caddy` 即可。

---

## 6. 配额触发 / RATE_LIMITED 处理

### 是什么

Anthropic 给 Pro/Max 订阅设了**会话级** + **日级** + **月级**多重限制。Claude Code 等客户端
触发限制时：

- 短期（5 分钟）触发：API 返 429，Clove 把账号标 `rate_limited` 暂时跳过
- 长期（小时/日）触发：可能返 403 + `error_type: "permission_denied"`

### Clove 的处理逻辑

参考 `app/services/account.py`：

- 收到 429 → 标记账号 `RATE_LIMITED`，记录 `resets_at`
- 后续请求自动避开这个账号（如果有别的账号就用别的；没有就给客户端返 429）
- 到达 `resets_at` 后自动恢复 `valid`

### 主动检查

```bash
ADMIN_KEY='...'
curl -sS -H "x-api-key: $ADMIN_KEY" http://127.0.0.1:5201/api/admin/accounts | \
  jq '.[] | {organization_uuid, status, resets_at}'
```

### 应对

- **临时**：等 `resets_at` 自动恢复
- **如果你重度使用**：考虑加第二个 Max 账号（见 §4 末尾）做负载分流
- **如果一直 429**：可能 Anthropic 风控警觉了，**减少请求频率**或者切到不那么激进的客户端

---

## 7. 升级 Clove 主线版本

upstream `mirrorange/clove` 主分支会持续更新。我们 fork 是为了：
- 自定义 Python 版本要求（3.13）
- 自定义 deploy 配置和 docs

升级时需要 rebase 我们的改动到新 upstream。

### 流程

```bash
cd /home/winbeau/clove

# 先在干净状态下做（确保没未 commit 的改动）
git status

# 加 upstream remote（仅第一次）
git remote add upstream https://github.com/mirrorange/clove.git 2>/dev/null

# 拉 upstream 最新
git fetch upstream

# 看 upstream 有什么新东西
git log HEAD..upstream/main --oneline

# 决定 rebase 还是 merge
# 推荐 rebase 保持线性历史
git rebase upstream/main

# 如果有冲突，按提示解决（通常在 pyproject.toml 之类）
# 解决后 git add . && git rebase --continue

# 同步 venv 依赖
uv sync --extra rnet

# 重启服务
sudo systemctl restart clove

# 验证还能用
sudo journalctl -u clove -n 20
SELF_KEY='...'
curl -sS -X POST -H "x-api-key: $SELF_KEY" -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-4-5","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:5201/v1/messages
```

### 数据不会丢

升级时**不会丢**的东西（它们在 .env 或 ~/.clove/data 里，不在仓库）：

- `~/.clove/data/accounts.json` — OAuth 账号
- `~/.clove/data/config.json` — admin/user keys 和其他设置
- `~/.clove/data/logs/` — 历史日志
- `/home/winbeau/clove/.env` — 部署配置（gitignored）

只要这些文件还在，升级后服务起来就跟原来一样。

### 万一升级后挂了

回滚：

```bash
# 看上一个工作的 commit
git log --oneline -10

# 切回去
git reset --hard <commit-id>
uv sync --extra rnet
sudo systemctl restart clove
```

或者先在 `git rebase upstream/main` 之前打个 tag，方便回滚：

```bash
git tag pre-upgrade-$(date +%Y%m%d)
# 升级 ... 出问题
git reset --hard pre-upgrade-20260429
```

---

## 8. 关键文件位置 cheat sheet

### 仓库内（受 git 管理）

```
/home/winbeau/clove/
├── pyproject.toml            # Python 依赖与元数据
├── uv.lock                   # 锁定的依赖版本
├── Makefile                  # make run / make install-dev
├── CLAUDE.md                 # 项目地图
├── README.md / README_en.md  # 项目主 README
├── docs/                     # 你正在读的文档
├── deploy/
│   ├── Caddyfile             # Caddy 配置模板
│   ├── caddy.service         # Caddy systemd unit
│   ├── clove.service         # Clove systemd unit
│   └── README.md             # deploy 子目录的 install 步骤
├── scripts/
│   ├── build_wheel.py        # 构建 wheel
│   └── oauth_bootstrap.py    # OAuth 引导脚本（教学/兜底）
├── tests/
│   └── test_*.py             # unittest 测试
├── app/                      # 业务代码
│   ├── main.py
│   ├── api/
│   ├── core/
│   ├── services/
│   └── processors/
└── .env                      # 部署配置（gitignored）
```

### 仓库外（系统级 / 用户级）

```
/etc/caddy/
├── Caddyfile                          # 从 deploy/Caddyfile install
└── secrets.env                        # 腾讯云 CAM 凭据（0640 root:winbeau）

/etc/systemd/system/
├── caddy.service                      # 从 deploy/caddy.service install
└── clove.service                      # 从 deploy/clove.service install

/var/lib/caddy/                        # Caddy state 目录
├── certificates/
│   └── acme-v02.api.letsencrypt.org-directory/
│       └── claude.selab.top/
│           ├── claude.selab.top.crt
│           └── claude.selab.top.key
└── acme/                              # ACME 账号、锁等

/usr/local/bin/caddy                   # xcaddy 编译的 Caddy 二进制

/home/winbeau/.clove/data/
├── accounts.json                      # OAuth 账号（含 access/refresh token）
├── config.json                        # API keys + admin keys + 其他设置
└── logs/
    └── app.log                        # Clove 文件日志（journalctl 也有）

/home/winbeau/.acme.sh/                # acme.sh（仅当你保留作兜底时有）
└── claude.selab.top_ecc/
    ├── claude.selab.top.cer
    ├── claude.selab.top.key
    ├── ca.cer
    └── fullchain.cer
```

### 朋友笔记本

```
~/.claude/
├── .credentials.json                  # OAuth token（从这里复制走的）
└── settings.json                      # 配置 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN
```

---

## 紧急断电 / 重新开机后的检查清单

VPS 重启后系统级服务自动起来了，但万一没起来时按这个跑一遍：

```bash
# 1. 服务都活着吗
sudo systemctl status caddy clove --no-pager | head -30

# 2. 端口监听对吗
sudo ss -tlnp | grep -E ':5201|:8443'

# 3. 简单 ping 一下
curl -sS http://127.0.0.1:5201/api/admin/accounts -H "x-api-key: $ADMIN_KEY" | jq length
curl -sS https://claude.selab.top:8443/api/admin/accounts -H "x-api-key: $ADMIN_KEY" | jq length

# 4. 真推理一发
SELF_KEY='...'
curl -sS -X POST -H "x-api-key: $SELF_KEY" -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-4-5","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}' \
  https://claude.selab.top:8443/v1/messages
```

第 4 步通了就是全部正常。挂在哪一步顺着上面 §1（看日志）排查。

---

## 相关文档

- [主文档：deployment.md](../deployment.md)
- [01: Cloudflare 与 OAuth bootstrap](01-cloudflare-bootstrap-saga.md)
- [02: TLS 证书与 :8443](02-tls-and-port-8443.md)
- [03: systemd 沙箱选项的两个隐藏坑](03-systemd-hardening-traps.md)

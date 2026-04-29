# Clove 部署经验主文档

> **写给谁看**：动手能力 OK，但 OAuth、Cloudflare、TLS 证书、systemd 这些名词对你还很模糊
> 的同学。我们尽量把"为什么这么做"讲透，再给"怎么做"。
>
> **怎么读**：从头到尾顺着读一遍，建立全局理解；后续遇到具体问题再去翻 [副文档](#8-副文档索引)。
> 一遍读完大概 30–45 分钟，看不懂的术语在 [§2 概念扫盲](#2-概念扫盲) 里都有解释。

---

## 目录

1. [这是什么？我们要解决什么问题？](#1-这是什么我们要解决什么问题)
2. [概念扫盲](#2-概念扫盲)
3. [整体架构](#3-整体架构)
4. [核心约束（决策驱动力）](#4-核心约束决策驱动力)
5. [最终方案](#5-最终方案)
6. [文件清单：我们改了什么](#6-文件清单我们改了什么)
7. [执行时间线](#7-执行时间线)
8. [副文档索引](#8-副文档索引)

---

## 1. 这是什么？我们要解决什么问题？

### 一句话概括

> 把一个 **Claude Pro/Max 月费订阅** 包装成 **标准 Anthropic API**，让你和朋友都能在自己的
> 电脑上用 Claude Code（或任何走 Anthropic SDK 的客户端）愉快地写代码。

### 为什么需要"包装"？

Anthropic 给你了两种买东西的方式：

| 类型 | 计费 | 凭据 | 谁在用 |
|---|---|---|---|
| **API key** | 按 token 用量 | `sk-ant-api03-...` 一串 key | 开发者、企业 |
| **订阅 Pro/Max** | 按月固定 | OAuth 登录 + 浏览器/Claude Code | 普通用户 |

订阅用户**没有 API key**——你能用的只是 claude.ai 网页和 Claude Code 客户端。如果你想：

- 写一段脚本调用 Claude
- 跟朋友共享你的订阅
- 接入 SillyTavern / Cline 等需要"标准 API"的客户端

就得自己搭一个 **反向代理**：客户端把请求发给你的代理（看起来像 Anthropic API），代理用你
的 OAuth token 转发到 Anthropic 真正的服务器，再把响应转回去。

[Clove](https://github.com/mirrorange/clove) 就是这样的反向代理。

### 为什么不直接用别人的 SaaS？

- **凭据不归你管** → 别人的服务器掌握你账号 OAuth token
- **被滥用 = 你的订阅被风控** → Anthropic 看到的是你账号在被异常调用
- **可能随时关停** → 这类灰色服务生命周期很短

自己搭一份成本很低（一台 5 美元 VPS + 一个域名），换来完全可控。

### 预期产物

朋友在他自己电脑上：

```bash
export ANTHROPIC_BASE_URL='https://你的域名:port'
export ANTHROPIC_AUTH_TOKEN='你给他的一把 key'
claude  # 进入 Claude Code 交互
```

就跟用官方 API key 一样。背后所有 OAuth 复杂度对客户端不可见。

---

## 2. 概念扫盲

### 反向代理 (reverse proxy)

**普通代理**（forward proxy）：你不想让目标网站看见你真实 IP，让代理替你转发请求。代理代表
**客户端**。比如 VPN、Shadowsocks。

**反向代理**：网站方面前面套一层服务器，所有用户请求先到反代，反代再决定怎么处理（转发到
真正的应用、加缓存、加 TLS 证书……）。代理代表 **服务端**。比如 Nginx、Caddy。

我们这套部署里：
- **Caddy** 是反向代理：客户端打 `https://claude.selab.top:8443`，Caddy 接收并转发到 Clove
- **Clove** 也是反向代理：拿到请求后，用 OAuth token 转发到 `api.anthropic.com`

### OAuth（vs API key、vs cookie）

三种 Claude 的"我能调你 API"凭据：

| 凭据 | 长这样 | 怎么获得 | 适用 |
|---|---|---|---|
| **API key** | `sk-ant-api03-XXX` | console.anthropic.com 后台生成 | 按量计费用户 |
| **session cookie** | `sessionKey=sk-ant-sid02-XXX` | 浏览器登 claude.ai 后从 DevTools 复制 | 模拟网页版 |
| **OAuth access token** | `sk-ant-oat01-XXX`（短）+ `refresh_token`（长） | 走 OAuth 授权流程 | 订阅用户、Claude Code |

OAuth 想象成：
> 你在某宝下单不输密码，而是"用微信登录"。微信弹个授权窗口"是否允许某宝读你的头像？"，
> 你点同意后微信发给某宝一个**只能干特定事**的临时令牌（access_token），还有一个能换新令牌的
> 长期令牌（refresh_token）。某宝拿临时令牌调微信 API 干那些被授权的事。

PKCE 是 OAuth 里防止令牌被中间人偷走的小机制：客户端先生成一个随机数 `verifier`，把它哈希一
下叫 `challenge`，发给 OAuth 服务器；服务器先返回授权码 `code`，等客户端拿 `code + verifier`
来换 token 时，服务器验证 `sha256(verifier) == challenge`。中间人只能截到 `code` 和 `challenge`，
没有 `verifier` 就换不了 token。

### Cloudflare 的角色

[Cloudflare](https://www.cloudflare.com/)（简称 CF）是全球最大的 CDN + 反向代理服务。
Anthropic 把 `claude.ai` 和 `console.anthropic.com` 都放在 CF 后面，享受：

- **加速**：CF 在全球有节点，用户访问就近
- **抗 DDoS**：恶意流量被 CF 挡掉
- **机器人识别**：CF 通过 TLS 指纹、IP 信誉、User-Agent、JS 挑战等多维度判断"你是不是真人"

**对我们的影响**：VPS 出口 IP 通常被 CF 标为高风险（数据中心 ASN），直接 curl 会被挡。这个
约束**深刻影响了**我们 OAuth bootstrap 的设计——详见 [副文档 1](notes/01-cloudflare-bootstrap-saga.md)。

### TLS / HTTPS / 证书

HTTP 是明文协议；HTTPS = HTTP + TLS（传输层加密）。

为什么我们 endpoint **必须**是 HTTPS：

- 朋友的 API key 走在 HTTP 上 → 任何中间人（运营商、公共 Wi-Fi）都能截下来
- API key 一旦泄漏 → 你的整个订阅可能被滥用导致 Anthropic 风控

TLS 证书由 **CA**（Certificate Authority，证书颁发机构）签发，证明"这个域名属于这个公钥
持有者"。Let's Encrypt 是免费 CA，背后的协议叫 **ACME**（Automated Certificate Management
Environment）。

ACME 给 CA 证明"我真的拥有这个域名"有三种 challenge 方式：

| Challenge | 怎么证明 | 要求 |
|---|---|---|
| **HTTP-01** | 在你域名 80 端口放一个特定文件，CA 来下载验证 | 80 端口可达 |
| **TLS-ALPN-01** | 在 443 端口的 TLS 握手里塞特殊扩展 | 443 端口可达 |
| **DNS-01** | 在你域名添加一条特殊 TXT 记录 | DNS API 可调 |

DNS-01 特点：完全不依赖你机器上有没有可达端口。**对我们这种"443 被占用"的场景就是天然解**。

### systemd

Linux 上管"开机自启的服务"的标准工具。每个服务用一个 `.service` 文件描述：从哪儿启动、用哪
个用户、挂了要不要重起、读哪些环境变量、能写哪些目录……

我们这里用 systemd 跑两个服务：

- `clove.service` — 跑 Clove 主进程
- `caddy.service` — 跑 Caddy 反代

systemd 提供了一组**沙箱**选项（ProtectHome、ProtectSystem、ReadWritePaths……），让服务即使
被攻破也只能访问授权的目录。这套机制本身好，但配错了会让服务起不来——详见
[副文档 3](notes/03-systemd-hardening-traps.md)。

### Xray REALITY

Xray 是一个流行的代理工具集，REALITY 是它内置的一种协议：

- 代理客户端连进来，TLS 握手时假装在跟一个真实知名网站（比如 Microsoft.com）通信
- 真实流量装在握手里被识别后剥离出来转发给翻墙真实目的
- 没认证的流量"漏"到真的 Microsoft.com，看起来跟一个普通 Microsoft 用户没区别

**对我们的影响**：Xray REALITY 通常占着 443 端口，没法和别的 HTTPS 服务共存。这就是为什么
我们的 Caddy 跑在 :8443 而不是 :443。

---

## 3. 整体架构

### 数据流图

```
                                      朋友的笔记本
                                     ┌─────────────┐
                                     │ Claude Code │
                                     └──────┬──────┘
              ANTHROPIC_BASE_URL=https://claude.selab.top:8443
              ANTHROPIC_AUTH_TOKEN=sk-clove-friend-xxx
                                            │
                                            ▼ HTTPS
┌────────────────────────────────────────────────────────────────┐
│ VPS  IP=23.165.40.14                                           │
│ ┌─────────────────────────────────────────────────────────┐   │
│ │ [public]  :8443 Caddy                                   │   │
│ │   - TLS 终结（cert via Let's Encrypt + 腾讯云 DNS-01）   │   │
│ │   - reverse_proxy → 127.0.0.1:5201                      │   │
│ └────────────────────────────┬────────────────────────────┘   │
│                              │ HTTP（loopback）                │
│ ┌────────────────────────────▼────────────────────────────┐   │
│ │ [internal] :5201 Clove (FastAPI / Python 3.13)          │   │
│ │   - 验 API key（自定义的 sk-clove-self / sk-clove-friend） │   │
│ │   - 用磁盘上的 OAuth token 调 api.anthropic.com         │   │
│ │   - 把响应转回客户端                                     │   │
│ └──────────┬──────────────────────────────────────┬───────┘   │
│            │                                      │           │
│            ▼                                      ▼           │
│  ~/.clove/data/                       /var/lib/caddy/         │
│  ├ accounts.json                      ├ certificates/         │
│  └ config.json                        └ acme/                 │
│                                                                │
│ ┌─────────────────────────────────────────────────────────┐   │
│ │ [public] :443 Xray REALITY (用户已有，平行运行)         │   │
│ └─────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────┘
                                            │ HTTPS（Bearer OAuth token）
                                            ▼
                                  api.anthropic.com
                                  （Anthropic 真正 Claude API）
```

### 组件职责表

| 组件 | 监听 | 职责 | 跑在哪儿 |
|---|---|---|---|
| Claude Code（朋友） | — | 普通 Anthropic API 客户端 | 朋友笔记本 |
| Caddy | `0.0.0.0:8443` | TLS 终结 + 反向代理到 Clove | VPS，systemd 管 |
| Clove | `127.0.0.1:5201` | API key 鉴权 + OAuth 转发 + 流式响应 | VPS，systemd 管 |
| Xray | `0.0.0.0:443` | 与本套部署无关，但占着 443 | VPS，systemd 管 |
| Let's Encrypt | — | 给 claude.selab.top 签发 TLS 证书 | 公网服务 |
| 腾讯云 DNSPod | — | 域名 NS；ACME DNS-01 通过它 API 加 TXT 记录 | 公网服务 |
| Anthropic | `api.anthropic.com:443` | 真正的 Claude 推理服务 | 公网服务 |

### 为什么是这个组合？

**为什么需要 Caddy？** Clove 自己只跑 HTTP，没 TLS 能力。我们需要一个能：

- 接管 HTTPS 流量
- 自动管理 LE 证书（包括续签）
- 友好处理 SSE 流式响应（Anthropic 的流式输出）

的反代。Caddy 默认全自动 HTTPS 是市面上最舒服的，所以选它。Nginx 也可以但要自己折腾 certbot。

**为什么 Clove 不直接监听公网？** 两个原因：
1. 它没原生 HTTPS（要靠前置反代）
2. 单进程，挂了响应会断；前面套 Caddy 至少 TLS 握手不会受影响

**为什么不用 Cloudflare 当反代？** CF 的 origin certificate 可以解决证书问题，但：
- 免费 CF 不允许通过它代理 *非标准 HTTP/S 端口*（443 / 8443 这些有限制）
- 我们的域名 NS 在腾讯云 DNSPod 上，搬到 CF 又是一坨工作

**为什么 Python 3.13 而不是 3.11？** Clove 仓库 `.python-version` 锁的是 3.13；用最新版能享
受性能改进 + asyncio 体验更顺。3.11 也行但既然项目作者推荐 3.13 就听他的。

---

## 4. 核心约束（决策驱动力）

整套部署有 **4 个硬约束**，理解了它们就理解了"为什么是这个架构"：

### 约束 1：VPS 出口 IP 被 Cloudflare 视为高风险

**症状**：从 VPS 上直接 `curl https://claude.ai/api/organizations` 拿到 `HTTP 403` +
`cf-mitigated: challenge` 头，body 是 CF 的"Just a moment..."JS 挑战页。

**原因**：CF 维护一个 IP 信誉库，数据中心 ASN（VPS 服务商常用的网段）默认评分低。普通
家庭宽带 IP 几乎不会被挑战。

**导致的设计决策**：

- **不走 cookie 模式**：Clove 还有一种"模拟 claude.ai 网页版"的工作模式，需要持续打
  claude.ai——CF 拦截会让这条路完全废掉
- **OAuth bootstrap 不能在 VPS 上做**：拿 OAuth token 的过程要先调 `claude.ai/api/organizations`
  和 `claude.ai/v1/oauth/.../authorize`，VPS 都进不去

详见 [副文档 1](notes/01-cloudflare-bootstrap-saga.md)。

### 约束 2：Port 443 被 Xray REALITY 占用

**症状**：`ss -tlnp | grep :443` 显示 Xray 进程在监听。

**原因**：Xray REALITY 通常默认绑 443，因为这样混在普通 HTTPS 流量里更难识别。

**导致的设计决策**：

- Caddy 不能绑 :443，只能用别的端口（最终选 :8443）
- ACME 自动签证书三种 challenge 里，HTTP-01（要 80）和 TLS-ALPN-01（要 443）都没法用，**只能
  走 DNS-01**

详见 [副文档 2](notes/02-tls-and-port-8443.md)。

### 约束 3：域名 NS 在 DNSPod 国际版（dnspod.com）

**症状**：第一次用 acme.sh 的 `dns_dp` 插件签证书报 `invalid domain`。

**原因**：DNSPod 是腾讯旗下产品，但**国内版（dnspod.cn）**和**国际版（dnspod.com）**完全是两个
独立系统：账号体系、API endpoint、API token 都不互通。一个域名要么在 .cn 那边、要么在 .com，
不能跨。

**导致的设计决策**：

- acme.sh 必须用 `dns_dpi`（国际版插件，i = International）而不是 `dns_dp`
- 后续切到 Caddy 自己签证书时，用 `caddy-dns/tencentcloud` 插件（走腾讯云 CAM 凭据，覆盖国际版）

### 约束 4：Pro/Max 订阅没有 API key + 客户端会高并发调工具

**症状**：用 Claude Code / Cline 接 Clove 跑复杂任务，请求量大、并行多。

**Clove 的两种工作模式**：

1. **OAuth 模式**：直接调 `api.anthropic.com`，享受官方 API 全部能力 + 高并发
2. **网页反代模式**：用 cookie 模拟 claude.ai 网页版，只支持低并发

**导致的设计决策**：

- 走 OAuth 模式（约束 1 也要求避开 CF）
- 客户端用 Pro/Max 订阅时 OAuth 是天然路径

---

## 5. 最终方案

每条决策与上面 4 个约束的对应关系：

| 决策 | 怎么做 | 解决了什么约束 |
|---|---|---|
| Python 3.13 + uv 管理项目 | `uv sync --extra rnet`，pyproject.toml 锁 `>=3.13` | 工具链现代化 |
| OAuth-only（不开 cookie 路径） | 不在 `.env` 里设 `COOKIES`；admin UI 也不加 cookie 账号 | 约束 1 + 4 |
| OAuth bootstrap 复用本地 Claude Code | 笔记本上 `cat ~/.claude/.credentials.json`，转字段名 POST 给 Clove | 约束 1 |
| Caddy 跑 :8443 | 自定义端口 + `auto_https disable_redirects` + 静态站点 | 约束 2 |
| Caddy DNS-01 via tencentcloud | xcaddy 自定义编译 + caddy-dns/tencentcloud 插件 | 约束 2 + 3 |
| systemd 守护 + 沙箱 | `clove.service` + `caddy.service`，ProtectHome=read-only | 工程稳定性 |

### 操作流程速览

> 实际命令的细节都在副文档里。这里只列动作顺序方便你建立全局印象。

1. **VPS 上安装 Clove + 启动**：装 uv → `uv sync --extra rnet` → 启动 → 拿临时 admin key
2. **生成永久 keys**：通过 admin API 写入 admin / self / friend 三把 key 到 `config.json`
3. **本地笔记本拿 OAuth token**：从 `~/.claude/.credentials.json` 复制（前提：你笔记本上 Claude
   Code 已经登录过同一个账号）
4. **POST 给 Clove**：把 OAuth token + organization_uuid + capabilities 拼成 JSON，POST 给
   `/api/admin/accounts`
5. **smoke-test**：直接 curl Clove `127.0.0.1:5201/v1/messages` 验证 OAuth 链路通
6. **签 TLS 证书**：xcaddy 编译带 tencentcloud 插件的 Caddy → 写 `/etc/caddy/secrets.env` →
   Caddy 启动后自动跑 ACME DNS-01
7. **systemd 化**：把 Clove 和 Caddy 都用 `.service` 文件接管，开机自启
8. **打开云厂商安全组 8443 入站**
9. **朋友机器上配置环境变量** + 验证

---

## 6. 文件清单：我们改了什么

### 仓库内（已 commit）

| 路径 | 性质 | 用途 |
|---|---|---|
| `pyproject.toml` | 改 | `requires-python = ">=3.13"` 并精简 classifiers |
| `uv.lock` | 改 | uv 同步生成 |
| `Makefile` | 改 | `install-dev` 改成 `uv sync --extra rnet`；`run` 改成 `uv run python -m app.main` |
| `CLAUDE.md` | 新 | 给未来 Claude 看的项目地图 |
| `app/static/index.html` | 新（gitignored） | hatch `force-include` 要求 `app/static/` 存在 |
| `deploy/Caddyfile` | 新 | Caddy 配置：8443 + TLS via tencentcloud |
| `deploy/caddy.service` | 新 | Caddy systemd 单元 |
| `deploy/clove.service` | 新 | Clove systemd 单元 |
| `deploy/README.md` | 新 | deploy 子目录的 runbook |
| `scripts/oauth_bootstrap.py` | 新 | PEP 723 自包含 OAuth 引导脚本（最终没用上但保留） |
| `docs/` | 新 | 你正在读的文档树 |
| `.gitignore` | 改 | 加 `/deploy/certs/` |

### 服务器系统级（不在 repo 里）

| 路径 | 内容 | 谁管 |
|---|---|---|
| `/home/winbeau/clove/.env` | HOST/PORT/DATA_FOLDER/log 配置 | 你手动 |
| `/etc/caddy/Caddyfile` | 从 `deploy/Caddyfile` install 的副本 | 你手动 |
| `/etc/caddy/secrets.env` | `TENCENTCLOUD_SECRET_ID` + `_KEY` | 你手动 |
| `/etc/systemd/system/clove.service` | 从 `deploy/clove.service` install | 你手动 |
| `/etc/systemd/system/caddy.service` | 从 `deploy/caddy.service` install | 你手动 |
| `/var/lib/caddy/` | Caddy 状态（证书、ACME 账号、锁） | Caddy 自己 |
| `/home/winbeau/.clove/data/accounts.json` | OAuth 账号（access/refresh token） | Clove 自己 |
| `/home/winbeau/.clove/data/config.json` | API keys、admin keys、其他设置 | Clove 自己 |
| `/home/winbeau/.clove/data/logs/app.log` | Clove 日志 | Clove 自己 |
| `/usr/local/bin/caddy` | 你 xcaddy 编译出来的 Caddy 二进制 | 你手动 |

### 笔记本（用户本地）

| 路径 | 内容 |
|---|---|
| `~/.claude/.credentials.json` | Claude Code 的 OAuth token（我们从这里复制走的） |

---

## 7. 执行时间线

按真实对话顺序记录，便于事后复盘。每条 1–3 行。

1. **02:00** — `/init` 生成 `CLAUDE.md`；规划部署方案，确认走 OAuth + VPS 已有项目副本
2. **02:10** — 切换工具链到 uv + Python 3.13；`pyproject.toml requires-python` 升 3.13；
   `uv sync --extra rnet` 第一次 build 失败（hatch 要求 `app/static` 存在），加占位 index.html
3. **02:15** — `make run` 启动；日志里抓到临时 `sk-admin-xxxxx`；用它通过 admin API 灌入永久
   admin key + 两把用户 key（self / friend），写到 `~/.clove/data/config.json`
4. **02:20** — 第一次试加 OAuth 账号：cookie 模式，`POST /api/admin/accounts` →
   `400124 ClaudeAuthenticationError`。日志显示 GET `claude.ai/api/organizations` 返回 403
5. **02:25** — 诊断：在 VPS 上跑 rnet+chrome impersonation 复现，看到完整响应：
   `cf-mitigated: challenge` + HTML "Just a moment..."。**确认 CF 拦截，不是 cookie 失效**
6. **02:30–02:55** — 写 `scripts/oauth_bootstrap.py`：用 curl_cffi 在笔记本上跑 PKCE 流程
   - **失败 1**：只贴 sessionKey → 笔记本也 403（CF 挑战）
   - **失败 2**：贴完整 Cookie header（含 cf_clearance）→ 还是 403
   - **失败 3**：加上从 DevTools 抄来的 User-Agent + chrome131 impersonation → 步骤 1+2 通了
     （拿到 org_uuid + auth_code），步骤 3 卡在 `console.anthropic.com/v1/oauth/token` 还是 403
7. **03:00** — 改 fallback：脚本失败后打印一段 JS，让用户在浏览器 DevTools 跑
   - **失败 4**：浏览器 fetch 在 `platform.claude.com` 跨域到 `console.anthropic.com` →
     CORS 拒绝；同时 token 端点 429（前面试错次数太多被限流）
   - 发现 `console.anthropic.com` 已被 301 到 `platform.claude.com`
8. **03:05** — 转向：让用户从笔记本 `~/.claude/.credentials.json` 复制 Claude Code 现成的
   OAuth token（client_id 同 Clove 用的，可直接复用）。10 秒搞定
9. **03:08** — 转换字段名（claudeAiOauth.{accessToken,refreshToken,expiresAt}）→ Clove 需要
   的（oauth_token.{access_token,refresh_token,expires_at}），expires_at 从毫秒除 1000 转秒。
   POST `/api/admin/accounts` → 返回 `is_max: true`, `auth_type: oauth_only`, `status: valid` ✓
10. **03:10** — Smoke-test：`POST /v1/messages` 返回 `pong` JSON，链路通；日志确认走 OAuth path
11. **03:15** — 处理网络层：DNS 已经指向 VPS，但 :443 被 Xray 占用。讨论后选 :8443 + DNS-01
12. **03:20** — 第一轮 ACME：用 acme.sh + DNSPod，先试 `dns_dp` 报 `invalid domain` →
    确认域名在国际版 → 切 `dns_dpi`（DPI_Id/DPI_Key）→ Cert success ✓
13. **03:25** — 用户提议改用 `caddy-dns/tencentcloud`（更现代、内置续签）。`xcaddy build` 出
    自定义 Caddy 二进制；写 `/etc/caddy/secrets.env`；落 Caddyfile + caddy.service
14. **03:35** — 第一次启动 Caddy：crash on `mkdir /home/winbeau/.local/share/caddy:
    read-only file system`。是 systemd `ProtectHome=read-only` 屏蔽了 Caddy 默认 storage 目录
    - 修法：改 storage 到 `/var/lib/caddy` + 设 `ReadWritePaths` + `XDG_*_HOME` 环境变量
15. **03:40** — Caddy 起来 → ACME DNS-01：第一次 NXDOMAIN（TXT 还没传播），自动重试 → staging
    验证通 → prod 验证通 → `certificate obtained successfully` ✓
16. **03:45** — 启 `clove.service`：crash on `Could not acquire lock ... ~/.cache/uv/.tmp...`。
    `uv run` 想写 cache，但 ProtectHome 拦了
    - 修法：ExecStart 直接调 venv 的 python，不走 uv run
17. **03:50** — Clove 起来 → 端到端 curl `https://claude.selab.top:8443/v1/messages` 返回 pong ✓
18. **03:55** — 用户在外网机器上 curl 验证公网可达 → 整套部署完成 ✓

---

## 8. 副文档索引

按"你遇到什么问题"分类：

- **OAuth 加账号失败 / 401 / 403 / 拿不到 token**
  → [01 - Cloudflare 与 OAuth bootstrap 反复折腾的经过](notes/01-cloudflare-bootstrap-saga.md)

- **HTTPS 证书签不下来 / 端口冲突 / DNSPod 报 invalid domain**
  → [02 - TLS、:8443 与 ACME 选型](notes/02-tls-and-port-8443.md)

- **systemd unit 起不来 / read-only file system / 反复重启**
  → [03 - systemd 沙箱选项的两个隐藏坑](notes/03-systemd-hardening-traps.md)

- **日常运维：续证、改 key、加新账号、配额触发、升级 Clove**
  → [04 - 日常运维手册](notes/04-operations-runbook.md)

---

## 致谢与免责

- 项目本体：[mirrorange/clove](https://github.com/mirrorange/clove)（MIT）
- 文档结构启发：Diátaxis Framework 的 reference / how-to / explanation / tutorial 四象限
- 本部署方案仅供个人学习与好友间共享使用；商用或大规模分发请遵守 Anthropic ToS

如果你照着这套搭好了——告诉我，我就继续维护文档；如果哪一段读不懂，给我开 issue，我把概念
扫盲那段加厚。

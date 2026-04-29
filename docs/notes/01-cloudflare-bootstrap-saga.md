# 01 — Cloudflare 与 OAuth bootstrap 反复折腾

> **遇到什么 → 看这里**：
> - `400124 ClaudeAuthenticationError`
> - `cf-mitigated: challenge`、HTML "Just a moment..."
> - 笔记本上 curl_cffi 写 cookie 也被 403
> - DevTools fetch `console.anthropic.com` 报 CORS / 429
>
> **TL;DR**：Cloudflare 在 IP 层、TLS 指纹层、UA 层、Cookie 层都做了拦截。绕过它的稳定路径
> 是**完全跳过 OAuth bootstrap**，直接复用本地 Claude Code 已经存好的 OAuth token。

---

## 目录

1. [问题边界：bootstrap 这一步要做什么](#1-问题边界bootstrap-这一步要做什么)
2. [Cloudflare 是怎么挡的](#2-cloudflare-是怎么挡的)
3. [失败 1：VPS 直发 sessionKey](#3-失败-1vps-直发-sessionkey)
4. [失败 2：笔记本只贴 sessionKey](#4-失败-2笔记本只贴-sessionkey)
5. [失败 3：完整 Cookie 但 UA 不对](#5-失败-3完整-cookie-但-ua-不对)
6. [突破：UA 对齐 + chrome131 TLS 指纹](#6-突破ua-对齐--chrome131-tls-指纹)
7. [失败 4：第三步 console.anthropic.com 也被 CF 拦](#7-失败-4第三步-consoleanthropiccom-也被-cf-拦)
8. [失败 5：浏览器 fetch + 域名迁移 + CORS](#8-失败-5浏览器-fetch--域名迁移--cors)
9. [失败 6：form submit 绕 CORS](#9-失败-6form-submit-绕-cors)
10. [最终方案：复用本地 Claude Code 的 OAuth token](#10-最终方案复用本地-claude-code-的-oauth-token)
11. [学到的经验](#11-学到的经验)
12. [附录：脚本设计与 OAuth 时序图](#12-附录脚本设计与-oauth-时序图)

---

## 1. 问题边界：bootstrap 这一步要做什么

Clove 加 OAuth 账号是个**一次性**操作。你给它一个 cookie 或 OAuth token，它存到磁盘
（`~/.clove/data/accounts.json`）。之后每次推理请求 Clove 用这个 token 调
`api.anthropic.com`，跟 cookie 没关系。

### 用 cookie 加账号时，Clove 内部干了 3 件事

参考 `app/services/oauth.py:114-264`：

```
[1/3] GET https://claude.ai/api/organizations
       Headers: Cookie: sessionKey=sk-ant-sid01-XXX
       → 拿到 organization_uuid + capabilities

[2/3] POST https://claude.ai/v1/oauth/{org_uuid}/authorize
       Headers: Cookie: ..., Content-Type: application/json
       Body: { client_id, redirect_uri, scope, code_challenge, ... }
       → 拿到 redirect_uri 里包含的 auth_code

[3/3] POST https://console.anthropic.com/v1/oauth/token
       Headers: User-Agent: claude-cli/2.1.81 (external, cli)
       Body: { code, grant_type, code_verifier, ... }
       → 拿到 access_token + refresh_token
```

第一二步打 `claude.ai`（CF 后面）；第三步打 `console.anthropic.com`（也是 CF 后面）。**这两个
域名都受 CF 保护**，是后续所有麻烦的来源。

---

## 2. Cloudflare 是怎么挡的

### CF 的多层防御

| 层级 | 检查什么 | 命中怎么办 |
|---|---|---|
| **IP 信誉** | 这个 IP 是数据中心 / 已知扫描器 / 公开代理吗？ | 直接 challenge 或返回 4xx |
| **TLS 指纹（JA3/JA4）** | TLS 握手的 cipher 顺序、扩展、curve 等组合 | 不像主流浏览器 → challenge |
| **HTTP/2 fingerprint** | SETTINGS、stream priority 等 | 同上 |
| **User-Agent** | 带 UA 字符串特征 | 与 TLS 指纹不一致触发 challenge |
| **JS challenge** | 浏览器执行 CF 给的 JS、写 cookie | 通过 → 颁 `cf_clearance` cookie，再次访问凭它放行 |

### 关键 cookie

| 名字 | 作用 | 生命周期 |
|---|---|---|
| `__cf_bm` | Bot Management 短期 token | 30 分钟 |
| `cf_clearance` | "我已通过挑战" 长期凭证 | 几小时 |
| `_cfuvid` | 唯一访客 ID（用于关联多次请求） | 会话 |

**重要**：`cf_clearance` 不是单独存在的——它绑死 `(IP, User-Agent, TLS-JA4)` 三元组。把 cookie
复制到别的工具（不同 UA / 不同 TLS 指纹）就**立即失效**。

### `cf-mitigated` 响应头

CF 拦截时会在响应头里加这个，值常见有：
- `challenge`：JS 挑战页（HTTP 403 + HTML body）
- `block`：直接拒绝
- `bypass`：放行（少见）

我们整段经历几乎都在跟 `cf-mitigated: challenge` 打交道。

---

## 3. 失败 1：VPS 直发 sessionKey

### 操作

在 VPS 上 POST 到 Clove admin API 加 cookie 账号：

```bash
curl -X POST -H "x-api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d '{"cookie_value":"sessionKey=sk-ant-sid02-XXX"}' \
  http://127.0.0.1:5201/api/admin/accounts
```

### 错误

```json
{
  "detail": {
    "code": 400124,
    "message": "Authentication error. Please check your Claude Cookie or OAuth credentials..."
  }
}
```

Clove 日志：`AppException: ClaudeAuthenticationError - Code: 400124`。

### 诊断

400124 是 Clove 自己的"鉴权失败"包装。真实原因要复现底层调用：

```bash
uv run python <<'PY'
import asyncio
from app.core.http_client import create_session

async def main():
    async with create_session(timeout=30, impersonate="chrome", follow_redirects=False) as s:
        r = await s.request("GET", "https://claude.ai/api/organizations", headers={
            "Cookie": "sessionKey=sk-ant-sid02-XXX",
            "User-Agent": "Mozilla/5.0 (...)",
        })
        print('STATUS:', r.status_code)
        for k, v in r.headers.items():
            print('  ', k, '=', v)
asyncio.run(main())
PY
```

输出关键行：

```
STATUS: 403
  cf-mitigated = challenge
  content-type = text/html
  server = cloudflare
  cf-ray = 9f3d3e259d0d1476-LAX
```

**结论**：VPS 出口 IP 被 CF 直接挑战。`cf-mitigated: challenge` 头是铁证——这不是 Anthropic 的
"401 cookie 无效"，是 CF 在更外层就把请求挡了。

### 修法尝试

第一反应：换个工具（`rnet` 用 `Emulation.Chrome142`、`curl_cffi` 用 `impersonate="chrome"`）
→ 一样 403。**TLS 指纹伪装解决不了 IP 信誉问题**。

`oauth.py:69-70` 把这一层简单地映射成 `ClaudeAuthenticationError`：

```python
if response.status_code == 403:
    raise ClaudeAuthenticationError()
```

——所以 Clove 的报错信息有一定误导性。"Authentication error" 实际可能是 CF 拦截。

---

## 4. 失败 2：笔记本只贴 sessionKey

### 思路

VPS 进不去那就换笔记本。笔记本是住宅 IP，CF 不会无脑挑战。

### 操作

写了个自包含脚本 `scripts/oauth_bootstrap.py`，PEP 723 内联依赖，curl_cffi chrome 默认指纹。
让用户在笔记本运行，输入 sessionKey。

```bash
uv run /tmp/oauth_bootstrap.py
# 输入 sessionKey...
[1/3] GET /api/organizations ...
GET organizations failed: HTTP 403
first 200 bytes: '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment..."'
```

笔记本上**也**被挑战了。

### 诊断

CF 对**任何**新 session 都会先做一轮挑战，浏览器之所以"自动通过"是因为它有 JS 引擎能解
challenge，解完拿到 `cf_clearance`，之后所有请求带着 `cf_clearance` 就 bypass。

我们的脚本只贴了 `sessionKey`，没贴 `cf_clearance` —— CF 当然要重新挑战。

### 修法

让用户从浏览器 DevTools 复制**整段 Cookie header**（包括 `cf_clearance`、`__cf_bm` 等所有 CF
相关 cookie）。脚本提示词改成"贴整段 Cookie"。

---

## 5. 失败 3：完整 Cookie 但 UA 不对

### 操作

用户从 DevTools Network 里复制了那条 `/api/organizations` 请求的整个 Cookie 头：

```
anthropic-device-id=...; _fbp=...; sessionKey=sk-ant-sid02-XXX; _cfuvid=...;
cf_clearance=Hss9tyS_...; __cf_bm=roq.x_fwk...; ...
```

这次 `cf_clearance` 在了。重新跑脚本，**还是 403**。

### 诊断

`cf_clearance` 绑 `(IP, User-Agent, TLS-JA4)`。我们的脚本：

```python
"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
```

是个**通用旧 UA**，跟用户浏览器实际的 `Chrome/147.0.0.0` 差远了。CF 验证 cookie 时发现"这个
clearance 是 Chrome 142 拿的，但你现在 UA 是个不知道哪年的 Mozilla"，立即作废。

### 关键洞察

`cf_clearance` 不是 universal 通行证，它是个**绑定签名**。复制 cookie 时必须把当时的 UA 一起
复制过来用。

---

## 6. 突破：UA 对齐 + chrome131 TLS 指纹

### 修法

脚本改成接受两个输入：完整 Cookie + 完整 User-Agent（从 DevTools 同一个请求抄）；
curl_cffi 把 impersonation profile 换成 `chrome131`（最接近现代 Chrome 的 JA4）。

```python
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

with requests.Session(impersonate="chrome131", timeout=30) as s:
    r = s.get(url, headers={**headers, "User-Agent": user_agent})
```

### 结果

```
[1/3] GET /api/organizations (impersonate=chrome131) ...
      org_uuid=e3fe8dc5-ca5b-493b-95ba-abe5d9c0325a capabilities=['claude_max', 'chat']
[2/3] POST /v1/oauth/<org>/authorize (PKCE) ...
[3/3] POST console.anthropic.com/v1/oauth/token ...
token exchange failed: HTTP 403
```

**步骤 1+2 通了**：拿到 `org_uuid` + `capabilities=['claude_max', 'chat']`（确认是 Max 账号），
完成 PKCE 拿到 auth_code。但**第三步 token 兑换又被 403 了**。

---

## 7. 失败 4：第三步 console.anthropic.com 也被 CF 拦

### 为什么 console.anthropic.com 不一样

第一二步打 `claude.ai`，cf_clearance 是为 `claude.ai` 这个 domain 颁的。第三步打的是
`console.anthropic.com`，**不同 domain，cf_clearance 不通用**。

而我们手上没有 `console.anthropic.com` 的 cf_clearance（用户浏览器没主动访问过 console）。

### 上游 Clove 怎么处理这一步

看 `app/services/oauth.py:82-103`：

```python
async def _token_request(self, url: str, data: dict) -> Response:
    """Plain (non-impersonating) POST to the OAuth token endpoint.

    console.anthropic.com/v1/oauth/token rejects requests that carry
    browser fingerprinting headers (User-Agent, Origin, TLS JA3).
    Using httpx here avoids the 429.
    """
    session = create_plain_session(timeout=settings.request_timeout, ...)
    response = await session.request(
        method="POST", url=url, data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "claude-cli/2.1.81 (external, cli)",
        },
    )
```

注意**反直觉的事**：上游故意用**不带浏览器指纹**的 plain httpx + 一个 `claude-cli/2.x` 的 UA。
原理是 Anthropic 在 console.anthropic.com 给"官方 CLI"开了通道——只要 UA 看起来像 claude-cli，
CF 不挑战。

### 但对我们不灵

VPS 上同时试了 plain httpx 和 curl_cffi 模拟 chrome 的两种姿势：

```bash
# plain httpx with claude-cli UA
uv run python -c "
import httpx
r = httpx.post('https://console.anthropic.com/v1/oauth/token',
    data={'grant_type': 'authorization_code', 'code': 'invalid'},
    headers={'User-Agent': 'claude-cli/2.1.81 (external, cli)'},
    timeout=15)
print(r.status_code, r.text[:200])
"
# → status 403, body 'Just a moment...'
```

**结论**：我们 VPS 的 IP 在 console.anthropic.com 也被 CF 标了。`claude-cli` UA 那条白名单
对**这个 IP**不生效。

笔记本同样：脚本试了两种姿势（plain + chrome131），都 403。

---

## 8. 失败 5：浏览器 fetch + 域名迁移 + CORS

### 思路

笔记本浏览器对 `console.anthropic.com` 是真能访问的（用户浏览器有该域名的 cf_clearance）。
那让脚本停在拿到 auth_code 之后，吐出一段 JS，让用户在浏览器 DevTools 跑 `fetch()` 完成
token 兑换。

```javascript
(async () => {
  const r = await fetch('https://console.anthropic.com/v1/oauth/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      code: 'XXX',
      code_verifier: 'YYY',
      client_id: '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
      ...
    }),
    credentials: 'include',
  });
  console.log(r.status, await r.text());
})();
```

### 用户在 DevTools 里跑，看到的错误

```
Access to fetch at 'https://console.anthropic.com/v1/oauth/token'
from origin 'https://platform.claude.com' has been blocked by CORS policy
POST https://console.anthropic.com/v1/oauth/token net::ERR_FAILED 429
```

两件事同时发生：

#### 8a. 域名被 301 到 `platform.claude.com`

用户访问 `https://console.anthropic.com` 时浏览器被 301 到 `https://platform.claude.com`
（Anthropic 在 2026 年初做的迁移）。所以用户那个标签页的 origin 实际上是
`platform.claude.com`，不是 `console.anthropic.com`。

#### 8b. CORS

JS 的 `fetch` 是跨域请求（origin = platform.claude.com，目标 = console.anthropic.com），需要
目标返回 `Access-Control-Allow-Origin` 头才能读响应。Anthropic 的 token 端点不返回该头 →
浏览器 block 读响应。

奇特的是：日志同时显示 `429 Too Many Requests`——说明请求实际上发出去了（CORS 的"simple
request"是先发再决定能不能读响应）。Anthropic 返回 429 是因为我们前面试错次数太多触发了限流。

### 修法尝试

让用户**手动导航**到 `https://console.anthropic.com/oauth/code/callback`（一个真实存在的 path）
希望保持 origin。结果用户回来说"被 301 到 `https://platform.claude.com/oauth/code/callback`
然后卡住"——所有 console.anthropic.com 的 path 都被永久重定向。

`console.anthropic.com` 这个 origin 实际上**已不复存在**给浏览器用——只剩 OAuth 的 API 端点
还在该域名下供后端调用。

---

## 9. 失败 6：form submit 绕 CORS

### 思路

CORS 是浏览器对 `fetch`/`XHR` 的限制。**form submit 不受同样限制**——form 提交后浏览器整个
导航到响应页面，不需要 origin 校验。

```javascript
const f = document.createElement('form');
f.method = 'POST';
f.action = 'https://console.anthropic.com/v1/oauth/token';
// hidden inputs for code, verifier, client_id, ...
f.submit();
```

理论上：浏览器 POST → console.anthropic.com 返回 JSON → 浏览器把 JSON 当成新页面渲染
（显示 raw text），用户从地址栏复制 JSON。

### 没真正测试就放弃了

这条路**还没正式跑**就被另一条更短的路替代了。但提一下原因，免得你以后想试：

- 浏览器在 form submit 时确实送 `Origin: https://platform.claude.com`
- console.anthropic.com 仍然可能拒绝跨域 POST（它后端可以独立做 origin 校验）
- 即使能成功，UI 体验很差（用户要从 JSON 渲染页面手动复制）

---

## 10. 最终方案：复用本地 Claude Code 的 OAuth token

### 关键洞察

回头看 Clove 配置：

```python
# app/core/config.py:235
oauth_client_id: str = Field(
    default="9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    ...
)
```

这个 `client_id` 不是 Clove 自己注册的——它是 **Claude Code 的官方 OAuth client_id**。Clove 只是
"借用"了 Claude Code 的身份做 OAuth 流程。

所以：**如果用户笔记本上已经装过 Claude Code 并登录过同一个账号，OAuth token 就已经在磁盘上
了**。我们根本不需要重新跑 OAuth bootstrap。

### Claude Code 的 token 在哪

| OS | 路径 |
|---|---|
| macOS / Linux / WSL | `~/.claude/.credentials.json` |
| Windows | `%USERPROFILE%\.claude\.credentials.json` |
| 也可能在 OS 凭据管理器（macOS Keychain / Linux libsecret / Windows Credential Manager） |

文件内容（用户实际复制下来的）：

```json
{
  "claudeAiOauth": {
    "accessToken": "sk-ant-oat01-poEyZ0G5...",
    "refreshToken": "sk-ant-ort01-aRtMueJ1...",
    "expiresAt": 1777485857825,
    "scopes": [
      "user:file_upload",
      "user:inference",
      "user:mcp_servers",
      "user:profile",
      "user:sessions:claude_code"
    ],
    "subscriptionType": "max",
    "rateLimitTier": "default_claude_max_20x"
  }
}
```

### 字段映射

Claude Code 用驼峰 + 毫秒时间戳；Clove 要下划线 + 秒。手动转一下：

```json
{
  "oauth_token": {
    "access_token": "<从 accessToken 复制>",
    "refresh_token": "<从 refreshToken 复制>",
    "expires_at": 1777485857.825   // expiresAt / 1000
  },
  "organization_uuid": "e3fe8dc5-ca5b-493b-95ba-abe5d9c0325a",
  "capabilities": ["claude_max", "chat"]
}
```

`organization_uuid` 和 `capabilities` 来自前面脚本步骤 1（`/api/organizations`）已经拿到的
值。如果当时没拿到，也可以在 claude.ai 网页 DevTools 里 `fetch('/api/organizations').then(r=>r.json())`
拿到。

### POST 给 Clove

```bash
curl -X POST -H "x-api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d "$BUNDLE" http://127.0.0.1:5201/api/admin/accounts
```

Clove 返回：

```json
{
  "organization_uuid": "e3fe8dc5-...",
  "capabilities": ["claude_max", "chat"],
  "cookie_value": null,
  "status": "valid",
  "auth_type": "oauth_only",
  "is_pro": true,
  "is_max": true,
  "has_oauth": true,
  ...
}
```

`auth_type: oauth_only` + `is_max: true` + `status: valid` ✓ — 一切就绪。

之后所有推理请求 Clove 调 `api.anthropic.com`，**不再触碰 claude.ai 也不触碰 console.anthropic.com**，
所以 Cloudflare 拦截到此终结。

---

## 11. 学到的经验

### 关于 Cloudflare

1. **CF 对数据中心 ASN 出口 IP 默认就警惕**——VPS 直接打 CF 后面的服务大概率被挑战
2. **TLS 指纹伪装解决"被识别为爬虫"，但解决不了"IP 信誉差"**——这是两个独立的判定层
3. **`cf_clearance` 是个绑定签名**，绑 `(IP, UA, TLS-JA4)` 三元组，复制时必须把所有要素一起带上
4. **CF 跨域不通用**——`claude.ai` 的 cf_clearance 在 `console.anthropic.com` 那边失效
5. **不要在 5 分钟内反复试**——CF/Anthropic 都会临时 IP 限流，越试越糟。失败一次先等 5 分钟
   冷却

### 关于 OAuth

6. **OAuth client_id 决定 token 接受范围**——同一个 client_id 颁的 token 在所有 server 处都被
   认作"那个客户端的请求"。Clove 借用 Claude Code 的 client_id，所以 Claude Code 已有 token
   可以无缝复用
7. **OAuth bootstrap 是一次性步骤**——别为这一次性步骤搭复杂工程；能借现成 token 就借
8. **PKCE 的设计很优雅**：verifier 不出客户端，code 即使被截也无法兑换 token

### 关于 API 设计

9. **底层错误向上汇报时容易丢信息**——Clove 把任何 403 都包成 `ClaudeAuthenticationError`，让
   用户误以为"cookie 失效"，其实可能是 CF。debug 时**永远要复现底层调用看真实状态码 + 头**
10. **域名迁移**：Anthropic 把 console.anthropic.com 301 到 platform.claude.com，影响所有靠
    "在 console 域名上跑 JS"的工具（包括我们这套）

### 关于工程取舍

11. **能用现成的就别从头跑**——这套折腾的精华是"绕开 OAuth bootstrap"，而不是"实现 OAuth
    bootstrap 的最佳姿势"
12. **写自包含工具脚本 + PEP 723 内联依赖**是好习惯——`scripts/oauth_bootstrap.py` 哪怕没用上，
    其实是排查 CF 拦截的最好工具

---

## 12. 附录：脚本设计与 OAuth 时序图

### `scripts/oauth_bootstrap.py` 设计要点

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["curl_cffi>=0.7"]
# ///
```

PEP 723 inline metadata 让脚本完全自包含，`uv run` 自动建临时 venv 装依赖。

关键参数：

- `IMPERSONATE = "chrome131"` — 接近现代 Chrome 的 TLS-JA4
- 接受**完整 Cookie 头**（含 cf_clearance）+ **完整 User-Agent**（与 cookie 同一请求抄）
- 三步流程明确分离，每步失败有具体诊断信息
- 最后一步失败时降级到"打印浏览器可执行的 JS 片段"

虽然最后没用上，作为"在受 CF 保护的 API 上跑 OAuth"的样板代码值得留着。

### OAuth + PKCE 完整时序图

```
Client (浏览器/CLI)              claude.ai                console.anthropic.com
  │                                 │                          │
  │ 1. 生成 PKCE                    │                          │
  │   verifier = random(32)         │                          │
  │   challenge = sha256(verifier)  │                          │
  │                                 │                          │
  │ 2. GET /api/organizations       │                          │
  │   Cookie: sessionKey=...        │                          │
  │ ──────────────────────────────► │                          │
  │ ◄──── { uuid, capabilities }    │                          │
  │                                 │                          │
  │ 3. POST /v1/oauth/{uuid}/authorize                          │
  │   { client_id, code_challenge=challenge, ... }              │
  │ ──────────────────────────────► │                          │
  │ ◄────── { redirect_uri:                                     │
  │           "https://console../callback?code=AUTH_CODE" }     │
  │                                 │                          │
  │ 4. POST /v1/oauth/token         │                          │
  │   { code=AUTH_CODE,             │                          │
  │     code_verifier=verifier,     │                          │
  │     grant_type=authorization_code }                         │
  │ ──────────────────────────────────────────────────────────► │
  │ ◄────── { access_token, refresh_token, expires_in }         │
  │                                 │                          │
  │ 5. 之后 推理调用直接走 api.anthropic.com，                  │
  │    Authorization: Bearer access_token                       │
```

PKCE 的精妙：服务端只见过 `challenge = sha256(verifier)`，最后 token 兑换时拿 `verifier` 验
`sha256(verifier) == challenge`。中间人即使截到了 `code` 和 `challenge`，没有 `verifier` 也
换不出 token。

---

## 相关文件

- `app/services/oauth.py` — Clove 内部的 OAuth 实现（`get_organization_info`,
  `authorize_with_cookie`, `exchange_token`）
- `app/api/routes/accounts.py` — admin 加账号的 HTTP 端点（`POST /api/admin/accounts`）
- `app/core/account.py` — `Account` 数据结构和 `is_pro` / `is_max` 判定
- `scripts/oauth_bootstrap.py` — 自包含的 PEP 723 OAuth 引导脚本（教学/兜底用）

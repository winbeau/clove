# 02 — TLS 证书、:8443 与 ACME 选型

> **遇到什么 → 看这里**：
> - 端口 :443 被另一个服务占着，HTTPS 怎么办
> - acme.sh 报 `invalid domain`
> - DNSPod 国内/国际版分不清
> - Caddy 第一次签证书 NXDOMAIN
> - `caddy-dns/dnspod` vs `caddy-dns/tencentcloud` 怎么选
>
> **TL;DR**：当 80/443 都拿不到时，DNS-01 是唯一选择；DNSPod 国际版要用 `dns_dpi`/腾讯云
> CAM API；Caddy 有 `caddy-dns/tencentcloud` 插件可以全自动管理证书。

---

## 目录

1. [Xray REALITY 占住 443 的本质](#1-xray-reality-占住-443-的本质)
2. [三种共存策略对比](#2-三种共存策略对比)
3. [ACME 三种 challenge 类型对比](#3-acme-三种-challenge-类型对比)
4. [DNSPod 国内版 vs 国际版的坑](#4-dnspod-国内版-vs-国际版的坑)
5. [acme.sh 路径如何走通](#5-acmesh-路径如何走通)
6. [为什么最终切到 caddy-dns/tencentcloud](#6-为什么最终切到-caddy-dnstencentcloud)
7. [xcaddy 编译流程](#7-xcaddy-编译流程)
8. [腾讯云 CAM 凭据](#8-腾讯云-cam-凭据)
9. [第一次 NXDOMAIN 但第二次成功的解释](#9-第一次-nxdomain-但第二次成功的解释)
10. [auto_https disable_redirects 的意义](#10-auto_https-disable_redirects-的意义)
11. [备选方案：Cloudflare Tunnel 的对比](#11-备选方案cloudflare-tunnel-的对比)
12. [Caddyfile 全文逐行注释](#12-caddyfile-全文逐行注释)

---

## 1. Xray REALITY 占住 443 的本质

Xray 是 [V2Fly/V2Ray](https://github.com/v2fly/v2ray-core) 的一个分支，REALITY 是它内置的协议
家族。跟我们这套部署相关的特性：

### REALITY 工作模式

```
互联网客户端
    │
    │ TLS handshake，SNI = www.microsoft.com（伪装）
    ▼
Xray :443
    │
    ├── 看 client hello 里的特殊字段 → 是不是已认证翻墙用户？
    │     是 → 把流量 unwrap 出来转发到真实代理目的（你的 Shadowsocks 后端等）
    │     否 → 把流量"裸"转发给真正的 microsoft.com（让它看起来跟普通用户没区别）
```

REALITY 的**本质是 TLS 拦截**——它必须看到所有 TLS 握手才能区分"翻墙用户" vs "假装用户"。

### 为什么不能跟 Caddy 共享 :443

- 共享 = 两个进程都 listen :443 → Linux 不允许（除非 SO_REUSEPORT，且应用得显式支持）
- "用 SNI 路由器把不同域名分发给 Xray / Caddy"：理论上可行（Caddy 自己有 `layer4` 插件
  支持 TCP+SNI 路由），但 REALITY 不希望它的 TLS 拦截被破坏
- "把 Caddy 跑到本地某端口，让 Xray 把 SNI=claude.selab.top 的流量 forward 给它"：可行但要
  改 Xray 配置，影响 REALITY 的 fallback 行为

最简单的策略就是**让 Caddy 用别的端口**。

---

## 2. 三种共存策略对比

| 策略 | 优点 | 缺点 | 我们选了吗 |
|---|---|---|---|
| 把 Xray 移到别处 | Caddy 可以用 :443，URL 干净 | 需要改 Xray 配置 + 客户端配置 + 可能动到 REALITY decoy | ❌ 用户不愿动 |
| Xray REALITY fallback 转发到 Caddy | URL 还是 :443 | Xray 配置复杂；REALITY 的 fallback 不直接支持基于 SNI 路由 | ❌ 太麻烦 |
| Caddy 用非标准端口（:8443） | 改动最小，立竿见影 | URL 带端口号略丑 | ✅ 这个 |
| 用 Cloudflare Tunnel 完全不绑端口 | 不需要本机暴露端口 | 域名 NS 要搬到 CF；Tencent CAM 要全切 | ❌ DNS 已在腾讯云 |

最终选 :8443。代价是朋友的 `ANTHROPIC_BASE_URL` 是 `https://claude.selab.top:8443` 而不是
`https://claude.selab.top`，这点小麻烦在主流客户端里都能配置。

---

## 3. ACME 三种 challenge 类型对比

证书签发要先证明你拥有这个域名。Let's Encrypt 支持三种证明方式：

### HTTP-01

```
LE 服务器 → http://你的域名/.well-known/acme-challenge/TOKEN
你 → 返回特定内容
```

**要求**：80 端口能从公网访问，能放静态文件。

### TLS-ALPN-01

```
LE 服务器 → tls://你的域名:443，ClientHello 里带 ALPN=acme-tls/1
你 → 在 TLS 握手里返回特殊扩展
```

**要求**：443 端口能从公网访问，TLS 服务器要能处理 ALPN。

### DNS-01

```
你 → 在你域名加一条 TXT 记录
LE 服务器 → 通过普通 DNS 查询验证
你 → 验证通过后删除 TXT 记录
```

**要求**：能通过 API 操作你域名的 DNS 记录。

### 我们的处境

- :80 没起 → HTTP-01 ❌
- :443 被 Xray 占用 → TLS-ALPN-01 ❌
- 域名在 DNSPod 上能 API 操作 → **DNS-01** ✅

DNS-01 还有一个很大的好处：**支持通配符证书**（`*.example.com`）。其他两种 challenge 不行。
我们这次只签了一个具体子域，但通配符的能力是 DNS-01 的杀手锏。

---

## 4. DNSPod 国内版 vs 国际版的坑

### 第一次失败：dns_dp 报 invalid domain

```bash
~/.acme.sh/acme.sh --issue --dns dns_dp -d claude.selab.top
[Wed Apr 29 03:19:12] Adding TXT value: ... for domain: _acme-challenge.claude.selab.top
[Wed Apr 29 03:19:14] invalid domain
[Wed Apr 29 03:19:14] Error adding TXT record to domain: _acme-challenge.claude.selab.top
```

API 调用层面"成功了"（200 响应），但响应 body 里说 `invalid domain`——意思是"凭你这个 token，
我看不到这个域名"。

### 根因：DNSPod 是两个独立产品

| 产品 | 域名 | API endpoint | 适用 |
|---|---|---|---|
| DNSPod 国内版 | dnspod.cn | dnsapi.cn | 中国大陆账号 + 国内域名注册商 |
| DNSPod 国际版 | dnspod.com | api.dnspod.com | 国际账号 + 海外域名 |

**账号体系完全独立**。一个 token 只能管理你**当前账号**下的域名。我们域名在国际版（NS 是
`*.dnspod.net`，这是国际版的 NS），但用了国内版的 token —— DNSPod 看不到 selab.top → invalid domain。

### acme.sh 的两个 plugin

| Plugin | 对应平台 | 环境变量 |
|---|---|---|
| `dns_dp` | DNSPod 国内 | `DP_Id` + `DP_Key` |
| `dns_dpi` | DNSPod 国际（International） | `DPI_Id` + `DPI_Key` |

**怎么辨认你域名在哪个平台**：
- 看 `dig +short NS yourdomain.tld`
  - `*.dnspod.cn` → 国内版
  - `*.dnspod.net` 或 `*.dnspod.com` → 国际版
- 或者两个 console 都登一下：哪边能看到这个域名就是哪边

我们的：

```bash
$ dig +short NS selab.top
dorado.dnspod.net.
karen.dnspod.net.
```

NS 在 `dnspod.net` → 国际版 → 必须用 `dns_dpi`。

---

## 5. acme.sh 路径如何走通

把 plugin 切成 `dns_dpi`、token 改成在 console.dnspod.com 生成：

```bash
unset DP_Id DP_Key
 export DPI_Id='你的国际版 ID'
 export DPI_Key='你的国际版 Key'

~/.acme.sh/acme.sh --set-default-ca --server letsencrypt
~/.acme.sh/acme.sh --issue --dns dns_dpi -d claude.selab.top
```

输出（精简）：

```
[Wed Apr 29 03:22:39] Le_LinkCert='https://acme-v02.api.letsencrypt.org/acme/cert/...'
[Wed Apr 29 03:22:40] Cert success.
[Wed Apr 29 03:22:40] Your cert is in: ~/.acme.sh/claude.selab.top_ecc/claude.selab.top.cer
[Wed Apr 29 03:22:40] Your cert key is in: ~/.acme.sh/claude.selab.top_ecc/claude.selab.top.key
[Wed Apr 29 03:22:40] The intermediate CA cert is in: ~/.acme.sh/claude.selab.top_ecc/ca.cer
[Wed Apr 29 03:22:40] And the full-chain cert is in: ~/.acme.sh/claude.selab.top_ecc/fullchain.cer
```

证书文件四个：

| 文件 | 作用 |
|---|---|
| `claude.selab.top.cer` | 你域名的叶子证书（leaf） |
| `ca.cer` | 中间 CA 证书（intermediate） |
| `fullchain.cer` | leaf + intermediate 拼起来（**Caddy/Nginx 要这个**） |
| `claude.selab.top.key` | 你的私钥（千万别泄漏） |

### 安装到稳定路径 + 续签 hook

```bash
~/.acme.sh/acme.sh --install-cert -d claude.selab.top --ecc \
  --key-file       /home/winbeau/clove/deploy/certs/claude.selab.top.key \
  --fullchain-file /home/winbeau/clove/deploy/certs/claude.selab.top.crt \
  --reloadcmd      'sudo systemctl reload caddy'
```

acme.sh 会：
1. 把 cert/key 拷到指定路径
2. 把 reloadcmd 注册到自己的内部数据库（`~/.acme.sh/account.conf`）
3. 之后每天 cron `acme.sh --cron` 检查所有域名，临近 60 天到期会自动续签
4. 续签成功后自动调用 reloadcmd → Caddy 优雅 reload，新证书生效

这条路径**完全可用**。我们后来切到 caddy 自己的 ACME 是因为更省心，不是这条路有问题。

---

## 6. 为什么最终切到 caddy-dns/tencentcloud

用户的考量：

1. **caddy-dns/dnspod 维护差**：作者好久没更新，issue 堆积；
   `caddy-dns/tencentcloud` 是社区新推的、active maintain
2. **腾讯云 API 是 DNSPod 的"接班人"**：腾讯收购 DNSPod 后，新的 CAM API（Tencent Cloud
   API 3.0）是统一的；老的 DNSPod 二十多个独立 API endpoint 慢慢被废弃
3. **Caddy 自己跑 ACME 更简洁**：不用 acme.sh + cron + reloadcmd 这条外部链路，少一个移动
   部件。Caddy 内部就有 [certmagic](https://github.com/caddyserver/certmagic) 库管理证书生
   命周期，掉电恢复、重试、状态机都更稳

切换的代价：要重新编译 Caddy（vanilla Caddy 没带 tencentcloud 插件）。一次性 30 秒搞定。

### 简单对比

| 方案 | 续签机制 | 故障点 | 切换/添加域名 | 调试 |
|---|---|---|---|---|
| acme.sh 外部 + 静态证书 | acme.sh 自己 cron | acme.sh / cron / reloadcmd 三处都可能挂 | 手动 issue + install-cert | acme.sh 日志在 `~/.acme.sh/acme.sh.log` |
| Caddy 自己 + DNS-01 plugin | Caddy 内部 certmagic | 只 Caddy 进程 | 改 Caddyfile + reload | journalctl -u caddy |

我们项目最后选了后者。

---

## 7. xcaddy 编译流程

### 装 Go

```bash
sudo apt update
sudo apt install -y golang-go
```

注意系统包管理器装的 Go 版本可能比较旧；如果 xcaddy 报 Go 版本太低，从 https://go.dev/dl/
拿最新的。

### 装 xcaddy

```bash
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
export PATH=$PATH:$(go env GOPATH)/bin
```

`xcaddy` 是个小工具，作用是"编译一个带特定 plugin 的 Caddy 二进制"。它内部：
1. 创建临时 Go 工程
2. 引入 Caddy 主仓 + 你指定的 plugin
3. `go build` 出最终二进制

### 编译

```bash
xcaddy build --with github.com/caddy-dns/tencentcloud
```

不到一分钟，当前目录下会有个 `caddy` 二进制。

### 验证

```bash
./caddy version              # v2.11.x ...
./caddy list-modules | grep -i tencentcloud   # 必须看到 dns.providers.tencentcloud
```

### 装到系统路径

```bash
sudo install -m 0755 -o root -g root ./caddy /usr/local/bin/caddy
caddy version    # 验证 PATH 找得到
```

### 失败的备选方案

我们试过用 Caddy 官方 build server 的 download API：

```bash
curl -fLG "https://caddyserver.com/api/download" \
  -d "os=linux" -d "arch=amd64" -d "p=github.com/caddy-dns/tencentcloud" \
  -o caddy
```

理论上更省事（不用装 Go），但实际试时 build server 返回 400（疑似 plugin 名解析问题）。
xcaddy 本地编译反而更稳。

---

## 8. 腾讯云 CAM 凭据

### 在哪生成

打开 https://console.cloud.tencent.com/cam/capi（CAM 访问密钥页），**新建**一对 SecretId + SecretKey。

### 需要哪些权限

CAM 用户必须有 DNSPod 写权限。最简单是绑定预设策略 `QcloudDNSPodFullAccess`。

### 写到 EnvironmentFile

```bash
sudo tee /etc/caddy/secrets.env >/dev/null <<'EOF'
TENCENTCLOUD_SECRET_ID=AKIDxxxxxxxxxxxxxxxx
TENCENTCLOUD_SECRET_KEY=yyyyyyyyyyyyyyyyyyyyy
EOF
sudo chown root:winbeau /etc/caddy/secrets.env
sudo chmod 0640 /etc/caddy/secrets.env
```

权限 `0640` 含义：root 读写，winbeau 组读，其他人无权限。Caddy 以 winbeau 身份跑，能读到。

### Caddyfile 引用

```caddy
tls {
    dns tencentcloud {
        secret_id {env.TENCENTCLOUD_SECRET_ID}
        secret_key {env.TENCENTCLOUD_SECRET_KEY}
    }
}
```

`{env.X}` 这种占位符 Caddy 启动时会从环境变量替换。环境变量从 `EnvironmentFile=` 来。

### 验证 systemd 真的把环境变量传进去了

```bash
sudo systemctl show caddy | grep Environment
# 应该看到 EnvironmentFile=/etc/caddy/secrets.env

# 看实际进程的环境变量（高级）
sudo cat /proc/$(pgrep -x caddy)/environ | tr '\0' '\n' | grep TENCENTCLOUD
```

---

## 9. 第一次 NXDOMAIN 但第二次成功的解释

### 现象（journalctl 节选）

```
03:39:57 obtaining new certificate for [claude.selab.top]
03:39:57 trying to solve challenge ... challenge_type=dns-01
         ca=https://acme-v02.api.letsencrypt.org/directory
03:40:04 challenge failed:
         "DNS problem: NXDOMAIN looking up TXT for _acme-challenge.claude.selab.top"
03:42:44 trying to solve challenge ... ca=https://acme-staging-v02...
03:42:51 authorization finalized: valid
03:42:54 trying to solve challenge ... ca=https://acme-v02 (prod 重试)
03:43:02 authorization finalized: valid
03:43:03 certificate obtained successfully ✓
```

第一次失败、第二三次成功。看起来很玄学，其实有迹可循。

### 时间线分解

1. **第一次（03:39:57）**：Caddy 调腾讯云 API 加 TXT 记录 → API 返回成功 → Caddy 立即通知
   LE 来验证（只等了 ~6 秒）
2. **LE 来查 DNS**：通过它自己的 DNS resolver 查 `_acme-challenge.claude.selab.top` 的 TXT
3. **结果 NXDOMAIN**：DNS 还没传播到 LE 用的 resolver 上
4. **Caddy 内部退避**：等了几秒后切换到 staging 环境再试
5. **第二次（03:42:44，约 3 分钟后）**：staging 通过 → 又试 prod → prod 也通过 ✓

### 为什么第一次会失败

DNS 传播是个分层缓存的过程：

```
腾讯云 API
    ↓ 立即生效（API 操作）
腾讯云权威 NS（dorado.dnspod.net 等）
    ↓ 数秒
Anycast 节点
    ↓ 数十秒
LE 的 resolver 缓存
    ↓ 5-30 秒
LE 验证服务
```

腾讯云 API 调成功后，记录在权威 NS 上立刻可见，但 LE 的查询 resolver 链路有自己的缓存，可能
先收到 NXDOMAIN 的负缓存（NXDOMAIN 也会被 cache，TTL 由 SOA 决定）。需要等几分钟传播开。

### Caddy 的处理

Caddy 用的 [certmagic](https://github.com/caddyserver/certmagic) 内置：

- **退避重试**：失败后等几秒到几分钟再试
- **prod / staging 双 CA 切换**：staging 没 rate limit，能多次试错；staging 通了再回 prod

所以**首次签证 NXDOMAIN 不要慌**，看后续日志会不会自动通过。如果 5 分钟后仍未通过，再去查
腾讯云 CAM 权限或 Caddy 配置。

### 怎么减少这种"首次失败"

- 在加 TXT 记录后**人为多等一会**（Caddy 默认会等，但可调长）
- 可以在 Caddyfile 里：

  ```caddy
  tls {
      dns tencentcloud { ... }
      propagation_delay 30s
      propagation_timeout 5m
  }
  ```

  我们没设这俩，默认值最终也通了。

---

## 10. auto_https disable_redirects 的意义

Caddy 有个超贴心的"全自动 HTTPS"特性，正常情况下它会：
1. 自动给所有 site 签证书
2. 在 :80 起一个 listener，把所有 HTTP 请求 301 到对应 HTTPS

但我们的场景：

- :80 没暴露在公网（或者没有也没人用 HTTP 访问）
- 我们用 :8443 不是 :443，"标准 HTTP→HTTPS"重定向的语义不适用

如果用默认 `auto_https on`，Caddy 启动时尝试 listen :80 → 没问题（unprivileged 用户也能 listen
非特权端口要 NET_BIND_SERVICE，Caddy 能拿到）但本地访问 80 没意义。

更糟的是 Caddy 会尝试 listen :80 + :443 做"automated HTTPS"，可能跟 Xray 撞 :443。所以我们：

```caddy
{
    auto_https disable_redirects
    admin off
    storage file_system /var/lib/caddy
}

claude.selab.top:8443 {
    tls {
        dns tencentcloud { ... }
    }
    ...
}
```

- `disable_redirects` 关掉 80→443 重定向
- 在 site address 里**显式写 :8443** → Caddy 只 listen 这个端口，不去抢 :443

`admin off` 是关掉 Caddy 自己的 admin API（默认 :2019），生产环境关掉减少暴露面。

---

## 11. 备选方案：Cloudflare Tunnel 的对比

### 是什么

[cloudflared](https://github.com/cloudflare/cloudflared) 是 CF 的反向隧道工具：

```
你的服务器 ─── 出站连接 ──► Cloudflare 边缘
                                  ▲
                                  │ 公网用户访问 yourdomain
                                  │
```

- 不用开任何入站端口
- TLS 在 CF 边缘终结
- 用 cloudflared 进程在你机器上拉 tunnel

### 优点

- VPS 完全不用 listen 端口（可以放心关防火墙）
- TLS 自动管理、无续签
- 抗 DDoS 顺便

### 缺点（对我们）

- **DNS 必须迁到 Cloudflare**：你的域名 NS 要从 dnspod 改到 cloudflare —— 牵动整个域名管理
- **CF 免费版有限制**：免费的 CF Tunnel 不允许某些非标端口走，且严禁高带宽视频流
- **跟 Anthropic CF 抗争的伦理问题**：你 outgoing CF tunnel + Anthropic 也在 CF 后面，CF 内部
  关系不可知

考虑后没采用。如果你 NS 本来就在 CF，可以走这条。

### 与我们方案的对比

| 维度 | Caddy :8443 + tencentcloud | Cloudflare Tunnel |
|---|---|---|
| 入站端口 | :8443 必须开 | 完全不用 |
| TLS 管理 | Caddy + LE 自动 | CF 自动 |
| DNS 提供商 | 任意（用 API 即可） | 必须 CF |
| 复杂度 | 中（一次编译 + 一次配置） | 低（装 cloudflared） |
| URL 形态 | `https://x.y.z:8443` | `https://x.y.z` |

---

## 12. Caddyfile 全文逐行注释

```caddy
# ==========================================================================
# 全局选项块（{} 不带域名的就是全局）
# ==========================================================================
{
    # 关掉 80→443 自动重定向。我们站点是 :8443，没标准重定向语义
    auto_https disable_redirects

    # 关掉 Caddy admin API（默认 :2019）。生产环境减少暴露面，要远程改配
    # 重新 reload 即可，不需要 admin API
    admin off

    # 把 Caddy 状态目录从默认的 ~/.local/share/caddy 改到 /var/lib/caddy。
    # 因为 systemd 单元里 ProtectHome=read-only 会屏蔽 home，详见 sub-doc 03
    storage file_system /var/lib/caddy
}

# ==========================================================================
# 站点块：claude.selab.top + 端口 :8443
# ==========================================================================
claude.selab.top:8443 {
    # ----------------------------------------------------------------------
    # TLS 证书管理：用 caddy-dns/tencentcloud plugin 走 DNS-01
    # ----------------------------------------------------------------------
    tls {
        dns tencentcloud {
            # 从环境变量读凭据（环境变量来自 systemd 的 EnvironmentFile）
            secret_id {env.TENCENTCLOUD_SECRET_ID}
            secret_key {env.TENCENTCLOUD_SECRET_KEY}
        }
    }

    # ----------------------------------------------------------------------
    # 响应压缩：节省带宽
    # ----------------------------------------------------------------------
    encode zstd gzip

    # ----------------------------------------------------------------------
    # 反向代理：所有请求转发到 Clove
    # ----------------------------------------------------------------------
    reverse_proxy 127.0.0.1:5201 {
        # 关键：Anthropic 流式响应是 SSE。flush_interval -1 表示"不缓冲，立刻
        # 把后端写入的字节 flush 给前端"，否则用户会感觉响应"卡一下才出来"
        flush_interval -1

        # 上游响应可以非常长（长 generation），把 timeout 提到 10 分钟
        transport http {
            read_timeout 600s
            write_timeout 600s
        }
    }

    # ----------------------------------------------------------------------
    # 日志：写到 stderr，由 systemd 抓进 journalctl
    # ----------------------------------------------------------------------
    log {
        output stderr
        format console
    }
}
```

如果你以后要加第二个域名（比如想给另一个朋友单独一个 endpoint），只要再加一个站点块就行：

```caddy
api2.selab.top:8443 {
    tls { dns tencentcloud { ... } }
    reverse_proxy 127.0.0.1:5202 {
        flush_interval -1
    }
}
```

---

## 相关文件

- `deploy/Caddyfile` — 仓库内的 Caddy 配置模板
- `deploy/caddy.service` — systemd 单元（详见 sub-doc 03）
- `deploy/README.md` — deploy 子目录的安装步骤
- `/etc/caddy/secrets.env`（系统） — 腾讯云 CAM 凭据
- `/var/lib/caddy/`（系统） — Caddy 状态目录

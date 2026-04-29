# 03 — systemd 沙箱选项的两个隐藏坑

> **遇到什么 → 看这里**：
> - 服务起不来，journalctl 里看到 `Read-only file system`
> - `clove.service` 反复 `auto-restart`、`exit-code 2`
> - `Could not acquire lock ... ~/.cache/uv/.tmp...`
> - `mkdir /home/winbeau/.local/share/caddy: read-only file system`
>
> **TL;DR**：systemd 的 `ProtectHome=read-only` 会同时干掉 uv 缓存目录和 Caddy 状态目录。
> 解法：要么显式 punch hole（`ReadWritePaths=`），要么把工具的目录改到 `/var/lib`。

---

## 目录

1. [systemd 单元的"沙箱"哲学](#1-systemd-单元的沙箱哲学)
2. [关键沙箱选项扫盲](#2-关键沙箱选项扫盲)
3. [第一坑：Caddy ProtectHome ↔ ~/.local/share/caddy](#3-第一坑caddy-protecthome--localsharecaddy)
4. [第二坑：clove.service 用 `uv run` ↔ ~/.cache/uv](#4-第二坑clove-service-用-uv-run--cacheuv)
5. [EnvironmentFile 加载敏感凭据的姿势](#5-environmentfile-加载敏感凭据的姿势)
6. [Type=notify vs simple vs forking](#6-typenotify-vs-simple-vs-forking)
7. [重启策略](#7-重启策略)
8. [学到的经验](#8-学到的经验)
9. [附录：两个 unit 文件全文逐行注释](#9-附录两个-unit-文件全文逐行注释)
10. [附录：诊断 unit 起不来的 checklist](#10-附录诊断-unit-起不来的-checklist)

---

## 1. systemd 单元的"沙箱"哲学

systemd 不只是个进程管家，它还是**最低成本的沙箱**。每个 `.service` 单元可以声明：

- **谁能跑**：`User=` `Group=`
- **能看到什么**：`ProtectHome=`、`ProtectSystem=`、`PrivateTmp=`
- **能写哪里**：`ReadWritePaths=`（在 ProtectXxx 上"开洞"）
- **能调哪些 syscall**：`SystemCallFilter=`
- **网络权限**：`PrivateNetwork=`、`RestrictAddressFamilies=`
- **能力**：`CapabilityBoundingSet=`、`AmbientCapabilities=`

**为什么要费这力气**：服务进程被攻破时，沙箱限制能阻止它读 SSH 密钥、写其他用户文件、用 root
权限做坏事。**默认值很宽松**——必须显式开启。

我们这次只用了一小部分（`ProtectHome` / `ProtectSystem` / `ReadWritePaths` /
`AmbientCapabilities`），就已经把自己绊倒两次。这背后的教训值得详细记一下。

---

## 2. 关键沙箱选项扫盲

### `ProtectHome={read-only|true|tmpfs|false}`

| 值 | 行为 |
|---|---|
| `false`（默认） | 不限制；服务能读写 `/home`、`/root`、`/run/user` |
| `read-only` | **可读不可写**：能读 home 里的文件（比如配置），不能改 |
| `true`（也叫 `yes`） | **完全不可见**：home 目录在服务视角下是空的 |
| `tmpfs` | home 目录被一个空 tmpfs 覆盖（用完即焚） |

我们两个服务都用的 `ProtectHome=read-only`：
- 服务能读 `/home/winbeau/clove/`（项目代码）
- 服务**不能写** `/home/winbeau/.cache/`、`/home/winbeau/.local/`、`/home/winbeau/.clove/data/` 等
- 加 `ReadWritePaths=/home/winbeau/.clove` 在第二条上开了洞

### `ProtectSystem={false|true|full|strict}`

| 值 | 行为 |
|---|---|
| `false`（默认） | 不限制 |
| `true` | `/usr` 和 `/boot` 只读 |
| `full` | 多加 `/etc` 只读 |
| `strict` | 整个系统目录只读（`/var` 也只读了，需要 `StateDirectory=` 才能写） |

我们用 `full`。`/var/lib/caddy` 默认还能写（`/var` 不在 full 的范围）。如果用 `strict` 就要
加 `StateDirectory=caddy` 让 systemd 自己管理一个写目录。

### `ReadWritePaths=/path1 /path2 ...`

在 `ProtectHome` / `ProtectSystem` 之上**开洞**——这些路径可写。

### `PrivateTmp=true`

服务有自己的 `/tmp`（系统其他进程看不见），关掉服务就清空。防止服务在 `/tmp` 留下敏感文件被
别的进程读。

### `AmbientCapabilities=CAP_NET_BIND_SERVICE`

普通用户能 listen 1024 以上的非特权端口（我们 :8443 完全 OK）。但 `CAP_NET_BIND_SERVICE` 让
普通用户也能 listen 1024 以下（如 :80、:443）。我们没用到 1024 以下，加这个是预防——以防以后
你想让 Caddy 切回 :443 时不需要改 unit。

### `Environment=KEY=VALUE` / `EnvironmentFile=path`

进程启动前的环境变量。`EnvironmentFile` 适合存 secrets（API key、token）——文件权限可独立
设，比写在 unit 里安全。

---

## 3. 第一坑：Caddy ProtectHome ↔ ~/.local/share/caddy

### 现场（journalctl）

```
Apr 29 03:39:57 caddy[627164]:
  {"level":"info","logger":"http","msg":"enabling automatic TLS certificate management",
   "domains":["claude.selab.top"]}

Apr 29 03:39:57 caddy[627164]:
  {"level":"error","msg":"unable to create folder for config autosave",
   "dir":"/home/winbeau/.config/caddy",
   "error":"mkdir /home/winbeau/.config/caddy: read-only file system"}

Apr 29 03:39:57 caddy[627164]:
  {"level":"error","logger":"tls","msg":"job failed",
   "error":"claude.selab.top: obtaining certificate: failed storage check:
            mkdir /home/winbeau/.local/share/caddy: read-only file system
            - storage is probably misconfigured"}
```

服务起来了（`enabling automatic TLS`），但马上想写两个目录都被拒：

| 路径 | Caddy 用它干嘛 |
|---|---|
| `/home/winbeau/.config/caddy` | "config autosave" — 把当前配置 dump 一份用于 reload 时回滚 |
| `/home/winbeau/.local/share/caddy` | 真正的 cert + ACME 状态存储 |

第一个是"warn"无关紧要，第二个是 fatal——没法存证书，整个 ACME 流程废了。

### 修法 A：放宽 ProtectHome（不推荐）

```ini
ProtectHome=false   # 把整个 sandbox 退化掉
```

简单粗暴，但放弃了沙箱保护——服务被攻破能读 SSH key 等敏感文件。**不推荐**。

### 修法 B：把 storage 改到 /var/lib/caddy（采用）

两步：

1. **Caddyfile 全局块**指定 storage：

   ```caddy
   {
       storage file_system /var/lib/caddy
   }
   ```

2. **systemd unit** 加 `ReadWritePaths` + 改 `XDG_*_HOME`：

   ```ini
   Environment=XDG_CONFIG_HOME=/var/lib/caddy
   Environment=XDG_DATA_HOME=/var/lib/caddy
   ReadWritePaths=/var/lib/caddy
   ```

   - `storage file_system` 决定 cert 写哪里（必要）
   - `XDG_CONFIG_HOME` 解决 "config autosave" 子坑（也写到 /var/lib/caddy）
   - `ReadWritePaths` 让 systemd sandbox 真的允许写（必要）

3. **创建目录**：

   ```bash
   sudo mkdir -p /var/lib/caddy
   sudo chown winbeau:winbeau /var/lib/caddy
   sudo chmod 0700 /var/lib/caddy
   ```

   重启 Caddy → ACME 通了 ✓

---

## 4. 第二坑：clove.service 用 `uv run` ↔ ~/.cache/uv

### 现场（journalctl）

```
Apr 29 03:46:58 uv[628229]: error: Could not acquire lock
Apr 29 03:46:58 uv[628229]:   Caused by: Could not create temporary file
Apr 29 03:46:58 uv[628229]:   Caused by: Read-only file system (os error 30)
                                          at path "/home/winbeau/.cache/uv/.tmpTKZxbe"
Apr 29 03:46:58 systemd[1]: clove.service: Main process exited, code=exited,
                            status=2/INVALIDARGUMENT
Apr 29 03:46:58 systemd[1]: clove.service: Failed with result 'exit-code'.
Apr 29 03:47:04 systemd[1]: clove.service: Scheduled restart job, restart counter is at 8.
```

服务循环失败 + auto-restart。问题：`/home/winbeau/.cache/uv` 想写，但 ProtectHome 拦了。

### 为什么 uv 想写 cache

`uv run` 每次调用都做：
1. 检查 lockfile 是否最新
2. 在 cache 里看 venv 是否存在 / 是否需要重建
3. **加文件锁**防止并发调用搞坏 cache
4. 才执行你给的命令

那个锁就在 `~/.cache/uv` 下。文件锁需要写权限。

### 修法 A：加 ReadWritePaths（中等）

```ini
ReadWritePaths=/home/winbeau/clove /home/winbeau/.clove /home/winbeau/.cache/uv
```

或：

```ini
Environment=UV_CACHE_DIR=/home/winbeau/clove/.uv-cache
ReadWritePaths=/home/winbeau/clove /home/winbeau/.clove
```

可行，但有点绕。

### 修法 B：跳过 uv，直接调 venv 的 python（采用）

```ini
ExecStart=/home/winbeau/clove/.venv/bin/python -m app.main
```

**根本不走 uv** —— uv 只在你 `uv sync` / `uv add` 时需要，**运行时不需要**。Clove 是个普通
Python 应用，venv 里的 python 就能直接跑。

更轻、启动更快、路径更明确。**强烈推荐这种**——只要你的工具是 venv-based。

---

## 5. EnvironmentFile 加载敏感凭据的姿势

### 文件权限

```bash
sudo chown root:winbeau /etc/caddy/secrets.env
sudo chmod 0640 /etc/caddy/secrets.env
```

`0640` 含义：

| Bit | 谁 | 权限 |
|---|---|---|
| 6 | owner（root） | rw |
| 4 | group（winbeau） | r |
| 0 | other | none |

Caddy 以 winbeau 身份跑，凭借 group 读权限拿到。其他用户读不到。这比 `0644`（其他人也能读）
安全得多。

### 文件格式

```
KEY=VALUE
ANOTHER_KEY=another_value
# 注释行
```

注意：

- **不要加引号**：`KEY="value"` 会让值变成 `"value"`（含引号）。除非你确实想要引号在值里
- 等号两边**不要空格**
- **不要导出**：写 `export KEY=value` 不行，systemd 不会执行 shell

### 验证 systemd 真的读到了

```bash
sudo systemctl show caddy --property=Environment --property=EnvironmentFiles
```

应该看到：

```
Environment=PATH=/usr/local/sbin:...
EnvironmentFiles=/etc/caddy/secrets.env (ignore_errors=no)
```

更直接的验证（看运行中进程的真实环境）：

```bash
sudo cat /proc/$(pgrep -x caddy)/environ | tr '\0' '\n' | grep TENCENTCLOUD
```

输出：

```
TENCENTCLOUD_SECRET_ID=AKIDxxxx
TENCENTCLOUD_SECRET_KEY=yyyy
```

---

## 6. Type=notify vs simple vs forking

`Type=` 影响 systemd 怎么判断"服务起来了"。

### simple（最简单）

```ini
Type=simple
ExecStart=/path/to/binary
```

systemd 一启动 binary 就认为"起来了"，立刻把状态改成 `active`。

**问题**：binary 还在初始化（如 ACME、连数据库）时就被认为"已启动"。后续 `systemctl
restart caddy` 不会等 ACME 完成才返回。

### notify

```ini
Type=notify
ExecStart=/path/to/binary
```

binary 必须主动调 `sd_notify(READY=1)` 通知 systemd "我准备好了"。systemd 等到这个信号才转
`active`。

**优势**：`systemctl restart` 真的等到服务可用才返回。

Caddy 支持 `Type=notify`（它内部调 sd_notify），所以我们用了。

### forking（老派）

```ini
Type=forking
ExecStart=/path/to/old-style-daemon
```

binary 启动后**自己 fork** 出 child 进程，parent 退出。systemd 跟踪 child PID。

老式 daemon（Apache、传统 nginx 在某些发行版上）用这个。新工具（Caddy、Clove）都不用。

### 我们的选择

- `caddy.service` → `Type=notify`（Caddy 原生支持）
- `clove.service` → 没指定（默认 `simple`），因为 uvicorn 不主动 sd_notify

---

## 7. 重启策略

### 我们的设置

```ini
Restart=on-failure
RestartSec=5
```

含义：
- `on-failure`：进程**非正常退出**（exit code 非零、被信号杀死）才重启；正常 exit 0 不重启
- `RestartSec=5`：失败后等 5 秒再重启，避免 crash loop 占满 CPU

### 还有几个值得了解的选项

| 选项 | 作用 |
|---|---|
| `Restart=always` | 不管怎么退出都重启（更激进） |
| `Restart=on-abnormal` | 只对信号死亡 / watchdog timeout 重启，普通非零 exit 不管 |
| `StartLimitBurst=3` `StartLimitInterval=60s` | 60 秒内连续失败 3 次后系统不再尝试，标记 `failed` |

### 我们 debug 时遇到的"reset-failed"

第二坑修完后，因为前面失败了 10+ 次累计了 restart counter，可能触发 StartLimit。需要：

```bash
sudo systemctl reset-failed clove
sudo systemctl restart clove
```

---

## 8. 学到的经验

### 配置层面

1. **任何 `ProtectHome=` 配合特定工具都要审一遍工具的默认目录**：
   - uv → `~/.cache/uv`
   - Caddy → `~/.config/caddy`、`~/.local/share/caddy`
   - npm → `~/.npm`
   - go → `~/go`
   - 几乎所有 CLI 工具都默认写 home，沙箱化时都要处理

2. **优先把工具状态目录搬到 /var/lib**，比 punch hole 更干净：
   - 配合 `ReadWritePaths=/var/lib/<tool>`
   - 配合工具自己的 `--data-dir` 参数 / `XDG_DATA_HOME` 环境变量

3. **运行时尽量不调 uv**：uv 是开发工具，运行时直接调 venv 的解释器更纯粹

### 调试层面

4. **journalctl 是 systemd 单元 debug 的首选**：
   ```bash
   sudo journalctl -u <name> --no-pager -n 50    # 最近 50 行
   sudo journalctl -u <name> -f                  # 实时跟流
   sudo journalctl -u <name> --since "5 min ago" # 时间范围
   ```

5. **`systemctl status` 只显示最近几行**：要看完整错误必须 `journalctl`

6. **Restart on-failure 会刷屏掩盖根因**：先 `systemctl stop`，关掉 auto-restart 后再 start
   一次仔细看错误，效率高得多

### 工程层面

7. **每加一个沙箱选项都要立即测一次**：systemd unit 不是声明式的"理想状态"，是一连串运行时
   约束，每条都可能挡一个不显眼的 syscall

8. **secrets 走 EnvironmentFile 而不是 unit 本身**：unit 文件经常 commit 进 repo，凭据不能在
   里面。EnvironmentFile 在系统级、独立权限管理

---

## 9. 附录：两个 unit 文件全文逐行注释

### `caddy.service`

```ini
[Unit]
# 这一段是单元的元信息和依赖关系
Description=Caddy fronting Clove on :8443 (cert via Tencent Cloud DNS-01)
Documentation=https://caddyserver.com/docs/

# 等"网络已就绪"事件，避免 Caddy 启动时 DNS / 路由还没好
After=network-online.target
Wants=network-online.target

[Service]
# Type=notify：Caddy 内部会调 sd_notify(READY=1)，systemd 等这个信号才认为服务起来
Type=notify

# 跑成谁。winbeau 是普通用户，没有不必要的 root 权限
User=winbeau
Group=winbeau

# 加载 secrets。文件权限 0640 root:winbeau，winbeau 通过 group 读到
EnvironmentFile=/etc/caddy/secrets.env

# 把 Caddy 默认的 home 目录指向 /var/lib/caddy
# 配合下面的 ReadWritePaths 让它能写
Environment=XDG_CONFIG_HOME=/var/lib/caddy
Environment=XDG_DATA_HOME=/var/lib/caddy

# 启动 Caddy。--environ 是把 env vars 也 dump 到日志开头方便诊断
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile

# 优雅 reload（比如证书续签后用）。--force 让 Caddy 即使配置没变也重新加载
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force

# 停止时最多等 5 秒，超时强杀
TimeoutStopSec=5s

# 提高文件句柄上限。Caddy 单进程会处理大量 TLS 连接
LimitNOFILE=1048576

# ----- 沙箱（最小权限） -----

# /dev 里只保留必要的设备，没/dev/sda 之类
PrivateDevices=true

# /home read-only。配合 XDG_*_HOME 改方向 + ReadWritePaths
ProtectHome=read-only

# /usr /etc /boot read-only
ProtectSystem=full

# 给 Caddy 写 storage 用
ReadWritePaths=/var/lib/caddy

# 允许 Caddy 绑 1024 以下端口（我们用 :8443 不需要，预防以后切 :443）
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
# 开机自启时挂在哪个 target 下
WantedBy=multi-user.target
```

### `clove.service`

```ini
[Unit]
Description=Clove Claude reverse proxy
Documentation=https://github.com/mirrorange/clove
After=network-online.target
Wants=network-online.target

[Service]
# 没指定 Type=，默认 simple。uvicorn 不主动 sd_notify，所以不要用 notify
Type=simple

User=winbeau
Group=winbeau

# 工作目录：Clove 启动时会从这里找 .env、locales 等
WorkingDirectory=/home/winbeau/clove

# 加载 .env 里的配置（HOST/PORT/DATA_FOLDER/log 等）
EnvironmentFile=/home/winbeau/clove/.env

# 关键：直接调 venv 里的 python，不走 uv run。原因见 §4
ExecStart=/home/winbeau/clove/.venv/bin/python -m app.main

# 进程异常退出时重启；正常 exit 0 不重启（让管理员主动启）
Restart=on-failure
RestartSec=5

# ----- 沙箱 -----
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
# 在 ProtectHome read-only 之上开两个洞：
#  - /home/winbeau/clove   — 仓库自身（venv、static 等都在这里，要可写）
#  - /home/winbeau/.clove  — Clove 数据（accounts.json, config.json, logs/）
ReadWritePaths=/home/winbeau/clove /home/winbeau/.clove
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
```

---

## 10. 附录：诊断 unit 起不来的 checklist

按顺序排查，前面的更常见：

1. **先看 journalctl**：
   ```bash
   sudo journalctl -u <name> --no-pager -n 100
   ```
   90% 的问题这里能直接看到。

2. **如果反复 auto-restart 信息淹没**：
   ```bash
   sudo systemctl stop <name>
   sudo systemctl reset-failed <name>
   # 编辑 unit 临时把 Restart=no
   sudo systemctl daemon-reload
   sudo systemctl start <name>
   sudo journalctl -u <name>  # 干净的失败现场
   ```

3. **ExecStart 路径错？** — 可以临时手动跑同样的命令：
   ```bash
   sudo -u <user> -g <group> /path/to/binary --args
   ```
   能跑通 → 多半 sandbox 选项的问题。

4. **Read-only file system**？看是哪个目录，要么改路径要么 `ReadWritePaths`。

5. **环境变量没读到**：
   ```bash
   sudo systemctl show <name> --property=Environment --property=EnvironmentFiles
   sudo cat /proc/$(pgrep -x <name>)/environ | tr '\0' '\n'
   ```

6. **依赖（After=、Requires=）有问题**：
   ```bash
   systemctl list-dependencies <name>
   ```

7. **systemd-analyze verify**（启动前静态检查）：
   ```bash
   sudo systemd-analyze verify /etc/systemd/system/<name>.service
   ```
   能查 unit 文件语法和大部分逻辑错。

---

## 相关文件

- `deploy/clove.service` — 仓库内的 Clove unit
- `deploy/caddy.service` — 仓库内的 Caddy unit
- `/etc/systemd/system/clove.service`、`caddy.service`（系统） — install 后的副本
- `/etc/caddy/secrets.env`（系统） — 腾讯云凭据
- `/var/lib/caddy/`（系统） — Caddy state 存放

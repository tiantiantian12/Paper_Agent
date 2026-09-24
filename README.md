# Paper Agent Server

Paper Agent 桌面端的账号服务端：QQ 邮箱验证码注册、邮箱密码登录、**大模型请求代理**，
以及一个独立端口的看板（注册用户 + 内置模型 + 密钥池 + 用户代理密钥 + **请求日志**）。

- **API 服务**：`http://127.0.0.1:8000` —— 桌面端注册登录 + 大模型代理
- **看板**：`http://127.0.0.1:8010` —— 浏览器打开，管理用户与密钥，查谁在打服务

## 快速开始

```bash
pip install -r requirements.txt
python run.py                 # 同时启动 8000 与 8010
python run.py --only api      # 只启动 API
python run.py --only admin    # 只启动看板
```

其它启动方式：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
uvicorn app.admin_app:app --host 0.0.0.0 --port 8010
```

启动后：

- 接口文档（Swagger）：<http://127.0.0.1:8000/docs>
- 用户看板：<http://127.0.0.1:8010>

## 注册登录流程

```
桌面端                                   服务端
  |-- POST /api/auth/send-code {email} -->  生成 4 位验证码（5 分钟有效，60s 冷却）
  |                                     |-- SMTP(smtp.qq.com:465) 发送验证码邮件
  |<-- 200 {"ok": true} ----------------|
  |-- POST /api/auth/register ---------->   校验验证码 + 建号 + 签发令牌
  |<-- {"token": "...", "user": {...}} -|
  |-- POST /api/auth/login {邮箱, 密码} ->   校验密码 → 令牌
  |<-- {"token": "...", "user": {...}} -|
```

## 接口一览（8000）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（默认**不记**进请求日志，避免监控刷屏） |
| POST | `/api/auth/send-code` | 发送 QQ 邮箱验证码（注册 / 重置密码） |
| POST | `/api/auth/register` | 邮箱 + 验证码 + 昵称 + 密码注册 |
| POST | `/api/auth/login` | 邮箱 + 密码登录 |
| POST | `/api/auth/login-code` | 邮箱 + 验证码快捷登录（未注册自动建号） |
| GET | `/api/auth/me` | 用 `Authorization: Bearer <token>` 换用户信息 |
| PATCH | `/api/auth/profile` | 更新昵称 / 头像（`{"nickname": "...", "avatar": "icon:moon"}`） |
| POST | `/api/auth/logout` | 失效令牌 |
| POST | `/api/llm/v1/chat/completions` | **大模型代理**（OpenAI 兼容，支持 `stream`），需带用户代理密钥 |
| POST | `/api/llm/v1/images/generations` | **文生图代理**（OpenAI 兼容），同一套用户密钥与供应商密钥池 |
| POST | `/api/llm/v1/videos` | **文生视频**：提交生成任务，返回供应商响应（含 `video_id`） |
| GET | `/api/llm/v1/videos/{video_id}?model_name=...` | 查视频任务结果（客户端每 1–2 秒轮询到 `completed` / `failed`） |
| GET | `/api/llm/user-key` | 用登录令牌换取（首次自动签发）自己的代理密钥 |
| GET | `/api/llm/models` | 当前可用的内置模型（已启用且配了密钥）：`model_id` / `name` / `desc` / `kind`，桌面端下拉框直接显示 |

看板接口（8010）：`GET /api/admin/users`、`GET /api/admin/stats`、`GET /api/admin/config`，
密钥管理 `/api/admin/models`、`/api/admin/provider-keys`、
`GET /api/admin/provider-keys/{id}/secret`（单取明文，供「复制」按钮用）、
`/api/admin/user-keys`、`/api/admin/llm-stats`、`GET /api/admin/model-health`（模型健康 / 限流）、
以及请求日志 `GET /api/admin/logs`、`GET /api/admin/logs/stats`、`GET /api/admin/logs/models`
（按模型看限流）、`GET /api/admin/logs/export`（CSV）、`DELETE /api/admin/logs`
（都要带 `X-Admin-Token`）。

## 请求日志（看板第三个页签）

每个请求都会留一条记录，回答**「谁、什么时候、打了哪个接口、结果如何」**——
服务被刷 / 被打的时候不至于「不知道是谁」：

| 列 | 内容 |
| --- | --- |
| 时间 | 本地时区，精确到秒 |
| 来源 IP | 默认是 TCP 对端地址（伪造不了）；挂了反代时见下面 `log.trust_proxy` |
| 账号 | 识别出的**昵称 / 邮箱**（带登录令牌或代理密钥的请求）；没有凭证就标「未识别」 |
| 凭证 | 打码后的来源凭证（`key pk-Adc33s…` / `token qp__yk7J…`），用来对上是哪把密钥在打；**明文绝不入库** |
| （看板请求） | `/api/admin/*` 走的是管理口令而不是账号令牌，账号列记作 **看板管理员**、凭证列记 `口令已验证` / `未带口令` / `口令不正确` —— 口令填错的访问照记，那本身就是要紧的信号 |
| 具体信息 | 这条请求在干什么：`对话 · 模型 agnes-3.0-flash · 流式 · 1 条消息`、`密码登录 · a@qq.com`、`文生视频 · 模型 … · 5s 720P` |
| 模型 | 打的是哪个模型（**单独一列**，不靠上面那句自由文本）——看板据此回答「是哪个模型在被限流」 |
| 结果 | 状态码、耗时、方法、路径、User-Agent（归类成 桌面端 / 浏览器 / 脚本） |

看板页签里给了四块：**来源排行**（按 IP 聚合，谁打得最多、有没有账号，点「只看它」直接筛）、
**接口排行**（在被猛打的是哪个接口——刷登录还是刷模型一眼看出来）、
**模型限流**（按模型聚合：哪个模型返回了 429、多少次、最后一次是什么时候；点「只看限流」
直接筛出这个模型的 429 明细）、**请求明细**（可按关键词 / 状态 / 时间窗 / 服务 / **模型**
筛选，支持 **导出 CSV 留证** 与清空）。

请求明细**分页**看：每页 50 / 100 / 200 / 500 条，底部「上一页 / 下一页 + 第 X / Y 页」，
不用一路往下滑（分页由服务端 `limit` / `offset` 支持）；左上「共 N 条」是当前筛选下的总数。
列多的时候**整张表横向滚动**，不会把「2026-09-23 03:13:52」这种值折成一列字；
模型已经有单独一列，所以「具体信息」里不再重复那句 `模型 xxx`（hover 仍能看到原文）。

### 哪个模型在被限流

「大模型与密钥」页签顶部还多了一块 **模型健康 · 限流**（`GET /api/admin/model-health`），
把三件事凑到一行里 —— 少一样就只能靠猜：

| 列 | 说什么 |
|---|---|
| 模型 / 类型 | 是对话、生图还是视频模型（免费档最常卡在**视频 / 生图**） |
| 密钥（可用/总） | 池子里还有几把能用；**冷却中 N 把**说明刚有钥匙命中限流被暂时跳过，下一轮会自动换一把 |
| 限流（429） | 这个模型近期返回了多少次 429，次数多的排在最上面 |
| 最近一次被限流 / 最近一次报错 | 什么时候卡的、供应商回的原话 |

应急办法就是给它**再加一把 Key**（「供应商密钥池」里添加），代理会自动轮换；
冷却时间由 `llm.provider_cooldown_seconds` 控制（默认 60 秒）。

细节与运维要点：

- **不影响请求速度**：日志先进内存队列，后台线程每 2 秒批量落库；队列满了丢最旧的并计数，
  看板上会红字提示「有 N 条被丢弃」——那个数字涨起来本身就是被刷的信号
- **真实来源**：服务前面挂了 Nginx / Cloudflare 时，把 `log.trust_proxy` 设为 `true`，
  改用 `X-Forwarded-For` 第一跳归因（原始头另存一列对照）。
  默认 `false`：只信 TCP 对端，别人伪造 XFF 也改不了归因
- **自动清理**：按 `log.retention_days`（默认 14 天）与 `log.max_rows`（默认 20 万条）
  定期清最旧的，不会把磁盘写爆
- **看板自己不刷屏**：GET `/api/admin/logs`、`/api/admin/logs/stats`（页面轮询用的那两个）
  不记录；其它看板操作（改模型、删密钥、清日志）照记，账号列显示「看板管理员」。
  只想看对外接口的话，把「服务」筛成**主服务**即可
- **桌面端与刷接口的区分**：桌面端请求带 `PaperAgent-Desktop/x.y` 的 User-Agent，
  日志里归成「桌面端」；裸 `Python-urllib` / curl 之类归成「脚本」。要更严的区分可以在
  前面那层反代上再加限速 / 封 IP

```bash
# 排查：某个 IP 最近 24 小时都打了什么
curl -H "X-Admin-Token: admin123" \
  "http://127.0.0.1:8010/api/admin/logs?ip=1.2.3.4&hours=24&limit=200"

# 导出一份 CSV 留证（Excel 直接打开）
curl -H "X-Admin-Token: admin123" -o logs.csv \
  "http://127.0.0.1:8010/api/admin/logs/export?hours=168&status=error"
```

## 大模型代理

内置模型（如 `sensenova-6.8-flash-lite`）**不直连供应商**，请求统一由服务端中转：

```
桌面端 ──(Bearer 用户密钥)──▶ /api/llm/v1/chat/completions ──(Bearer 供应商 Key)──▶ 供应商
```

- **供应商密钥只在服务端**：桌面端拿不到也不需要，Base URL 与 Key 都在看板「大模型与密钥」
  页签里维护，改完立即生效。
- **新账号自动发密钥**：注册（或首次验证码登录）时就签好一把，有效期默认 **30 天**
  （`llm.user_key_ttl_days`）；服务端会顺手把当前没有可用密钥的老账号也补上。
  桌面端登录后调 `/api/llm/user-key` 拿到的是同一把，不会重复签发。
- **用户密钥**：每个用户一把（可多把），服务端可设**有效天数**、续期、停用、删除；
  密钥不存在 / 已停用 / 已过期 / 无该模型权限的请求一律 401 / 403 挡在服务端，
  不会打到供应商。
- **密钥池轮换**：同一个模型可配多个供应商 Key，按「最久未用」优先轮流用；
  命中 `429` / 5xx 时把该 Key 冷却 `llm.provider_cooldown_seconds` 秒并自动换下一个
  （单次请求最多试 `llm.max_key_tries` 把）。
- **密钥复制**：密钥池列表只显示打码值（首尾各 4 位），行尾点「复制」才会按 id
  单取明文并写进剪贴板 —— 新加一个模型想复用旧模型的 Key 时，复制一下最省事。
- **流式透传**：`stream: true` 时供应商的 SSE 分片原样转发，桌面端边收边渲染。

常用操作：桌面端登录 → 服务端看板「注册用户」里点某个用户的「生成密钥」，密钥会自动
复制到剪贴板（也可以在看板「大模型与密钥 → 用户代理密钥」里统一签发 / 续期 / 停用）。
桌面端启动或登录时会自动调 `/api/llm/user-key` 把密钥取回本地缓存。

### 播种内置模型与密钥

`config.json` 的 `llm.models` 用来**播种**（每次启动都会跑一遍，幂等）：

```json
"llm": {
  "user_key_ttl_days": 30,
  "models": [
    {
      "model_id": "sensenova-6.8-flash-lite",
      "name": "SenseNova 6.8 Flash Lite",
      "desc": "内置模型 · 经服务端代理调用（需登录）",
      "kind": "chat",
      "base_url": "https://token.sensenova.cn/v1",
      "api_keys": ["sk-第一把", "sk-第二把"]
    },
    {
      "model_id": "sensenova-u1.5-lite",
      "name": "SenseNova U1.5 Lite",
      "desc": "文生图：输入提示词直接出图",
      "kind": "image",
      "base_url": "https://token.sensenova.cn/v1",
      "api_keys": ["sk-第一把"]
    }
  ]
}
```

``kind`` 决定这条模型走哪个代理接口：``chat`` → ``/chat/completions``，
``image`` → ``/images/generations``，``video`` → ``/videos`` 提交任务 + 结果查询
（三种都吃同一套用户密钥与供应商密钥池）。视频的时长 / 画幅由客户端在请求体里给，
分辨率固定 ``720P``。

- 模型不存在就创建；**密钥按 `api_key` 去重后补进密钥池**，所以写两把就是两把，
  重复启动不会产生重复条目
- 在看板里手工改过的地址 / 备注 / 停用过的密钥不会被播种覆盖（只有还停在代码里那版
  旧默认地址 `https://api.sensenova.cn/v1`、或备注还是空的时候，才会跟着
  `config.json` 补一次）
- 这里放的是**供应商密钥**（明文，和 SMTP 授权码一样属于服务端机密），
  看板只回显打码形式；不想写在文件里就留空 `api_keys`，去浏览器看板里手工添加

### 在服务端维护内置模型（下发给桌面端）

`GET /api/llm/models` 会把内置模型的 `model_id` / `name` / `desc` 下发给桌面端，
桌面端拿来填模型下拉框（名字一行、备注一行）：

```json
[{"model_id": "sensenova-6.8-flash-lite",
  "name": "商汤日日新 6.8",
  "desc": "国内直连 · 高性价比"},
 {"model_id": "gpt-4o",
  "name": "GPT-4o",
  "desc": "服务端新加的多模态模型"}]
```

所以看板「内置模型」里不管是点 **编辑**（显示名 / 备注 / Base URL / 启用状态一次改完）
还是 **添加模型**，桌面端都**不用发新版**：下次启动 / 重新登录同步过去就生效
（客户端缓存一份，退出登录时清掉）。

新增一个模型只要三步：

1. 看板「内置模型」里填 `model_id` / **类型（对话 / 文生图）** / 显示名 / 备注 /
   `base_url`，点「添加模型」
2. 在「供应商密钥池」里给它配至少一个 Key（**没配密钥的模型不会下发**）
3. 桌面端重新登录 → 下拉框的「内置模型」区就多出这一条，选中即用

**文生图模型**（类型选「文生图」）会走 ``/api/llm/v1/images/generations``：
桌面端选中它以后把输入当提示词直接出图，供应商那把 Key 只留在服务端 ——
之前这类模型的 Key 是写在桌面端源码里的，现在统一挪到看板维护，还能配多把轮换。

**文生视频模型**（类型选「文生视频」）同理，只是出片是异步的：桌面端先
``POST /videos`` 拿 ``video_id``，再轮询 ``GET /videos/{id}?model_name=...`` 直到
``completed``，然后下载落盘。供应商的限流 / 队列满（429 / 503）会原样透传给客户端，
界面上显示「服务繁忙，稍后再试」而不是网络故障。

（改完之后随时点行尾的 **编辑** 打开弹框调整——显示名、备注、Base URL、启用状态
在一个表单里改完再保存，不必按顺序一项项过。）

- 新增的模型和内置模型走**同一条代理链路**，桌面端不需要任何本地配置
- 桌面端本地清单里已有的 id（如 `sensenova-6.8-flash-lite`）只覆盖名称 / 备注，
  不会重复出现
- 新增的模型排在本地内置模型后面；显示名留空时用 `model_id`，备注留空时用一句
  默认提示，老库升级后本地那条的备注也不会被清空
- 停用或删掉模型：桌面端下次同步就看不到它，选中项会自动回退到别的可用模型
- 供应商地址与密钥不下发：桌面端只知道"用哪个模型"，请求仍由服务端转发

## 配置（config.json）

所有可调参数集中在项目根 `config.json`，改完重启服务即生效：

| 字段 | 当前值 | 说明 |
| --- | --- | --- |
| `smtp.smtp_host` / `smtp_port` | `smtp.qq.com` / `465` | 发信服务器 |
| `smtp.sender` | `153370967@qq.com` | 发件人邮箱 |
| `smtp.password` | `nifuhgrrhfsobgdb` | QQ 邮箱 **SMTP 授权码**（邮箱设置 → 账户 → POP3/SMTP 服务 → 生成授权码，不是登录密码） |
| `smtp.dry_run` | `false` | 置 `true` 时不真发信，验证码打印到服务端日志，方便本地联调 |
| `admin_access_token` | `admin123` | 看板访问口令；置空 `""` 则免口令 |
| `api_port` / `admin_port` | `8000` / `8010` | 两个服务端口 |
| `code.length` / `ttl_seconds` / `resend_interval` / `max_per_day` | `4` / `300` / `60` / `20` | 验证码策略 |
| `account.store_plain_password` | `true` | 是否额外保存一份明文密码供看板展示 |
| `account.allowed_email_domains` | `[]` | 限制注册邮箱后缀（注册接口始终要求 QQ 邮箱） |
| `data_dir` | `data` | SQLite 数据目录，相对路径按项目根解析 |
| `llm.user_key_ttl_days` | `30` | 新账号代理密钥的有效天数；`0` 表示不过期 |
| `llm.models` | 见下 | 内置模型与供应商密钥的播种清单（`model_id` / `name` / `desc` / `kind` / `base_url` / `api_keys`） |
| `llm.provider_cooldown_seconds` | `60` | 供应商密钥命中限流后的冷却秒数 |
| `llm.max_key_tries` | `3` | 单次代理最多试几把供应商密钥 |
| `llm.read_timeout` | `600` | 转发到大模型的读超时（流式是长连接） |
| `log.enabled` | `true` | 是否记录请求日志 |
| `log.retention_days` | `14` | 请求日志保留天数（`0` 表示不按时间清理） |
| `log.max_rows` | `200000` | 请求日志最多保留条数（超出删最旧的，`0` 表示不限） |
| `log.trust_proxy` | `false` | 前面挂了 Nginx / Cloudflare 时设 `true`，按 `X-Forwarded-For` 第一跳归因 |
| `log.skip_paths` | `["/health","/favicon.ico"]` | 不记录的路径 |

优先级：**环境变量 > `config.json` > 内置默认值**。想临时覆盖不必改文件，
用同名环境变量即可（`SMTP_PASSWORD`、`ADMIN_ACCESS_TOKEN`、`SMTP_DRY_RUN`、`API_PORT`、
`PAPER_AGENT_SERVER_DATA`、`PAPER_AGENT_SERVER_CONFIG` 指定其它配置文件路径）。

发信失败时注册接口返回 `502` 并带上具体原因。

> `config.json` 里含 SMTP 授权码，不要提交到公开仓库。

## 目录结构

```
config.json       集中配置（SMTP / 管理口令 / 端口 / 验证码策略）
app/
  config.py        读取 config.json 与环境变量
  database.py      SQLite 连接与建表
  security.py      密码哈希（PBKDF2）与令牌生成
  smtp_service.py  QQ 邮箱验证码邮件
  store.py         用户 / 验证码 / 会话仓储
  llm_store.py     内置模型 / 供应商密钥池 / 用户密钥仓储
  request_logs.py  请求日志：中间件 + 批量落库 + 查询 / 聚合 / 清理
  routers/auth.py  注册登录接口
  routers/admin.py 看板数据接口（含请求日志查询）
  routers/keys.py  看板密钥管理接口（模型 / 供应商密钥 / 用户密钥）
  routers/llm.py   大模型代理（OpenAI 兼容 + 取密钥）
  main.py          API 服务（8000）
  admin_app.py     看板服务（8010）
  web/             看板页面（index.html / style.css / main.js / keys.js / logs.js）
run.py             一键启动两个端口
```

几张表（`init_db` 自动建，首次启动会写入内置模型 `sensenova-6.8-flash-lite`）：

| 表 | 作用 |
| --- | --- |
| `llm_models` | `model_id` → 供应商 OpenAI 兼容地址（桌面端下拉框里的内置模型）；`name` / `desc` / `kind` 会下发给桌面端（`kind` 决定走对话 / 文生图 / 文生视频哪条链路） |
| `provider_keys` | 供应商 Key 池，含状态、冷却时间、最近出错信息 |
| `user_keys` | 用户代理密钥，含状态、有效期、允许的模型、最近使用时间 |
| `request_logs` | 请求日志（谁 / 什么时候 / 打了什么 / 结果），按 IP 与时间建索引，见「请求日志」一节 |

## 安全说明

- 登录校验只用 PBKDF2-HMAC-SHA256 哈希，数据库中另存一份明文副本**仅供看板展示**。
  生产环境请设置 `STORE_PLAIN_PASSWORD=false` 并为看板配置 `ADMIN_ACCESS_TOKEN`。
- 令牌有效期默认 30 天，落在 `sessions` 表，退出登录后即失效。
- 数据存在 `data/paper_agent.db`（SQLite，WAL 模式）。
- **供应商密钥只在服务端落库**，看板只回显打码后的形式（`sk-a******mnop`），
  桌面端永远拿不到；用户密钥可以随时停用 / 删除 / 设为过期，立即生效。
- 请求日志里同样只存**打码后的凭证**（`key pk-Adc33s…`），登录令牌与密钥明文不入库；
- 桌面端要**长视频**时，服务端会看到**多条** `POST /api/llm/v1/videos`（每条对应一段
  10–12 秒的生成，`mode` 为 `text` / `keyframe`）——那是客户端在分段续接，
  不是重复提交；请求日志的「具体信息」列能直接看出每段的时长与模式
  日志本身也受看板口令保护（`X-Admin-Token`）。
- 真要抗住 DDoS，服务前面还是得有一层（Nginx 限速 / Cloudflare / 云厂商的高防）：
  这里的作用是**出事时查得出是谁**，而不是替它挡流量。记得把 `log.trust_proxy`
  按实际部署调对，否则归因到的会是反代自己的 IP。

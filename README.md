# OCTOPUS CODEX

使用一个独立的飞书企业自建应用，持续对话并操作宿主机上的 Codex。

```text
飞书私聊 / 群聊 @机器人
          │ 官方 Python SDK 长连接
          ▼
Docker Compose
  bridge：Python 3.12 ───── db：PostgreSQL 17（持久卷）
          │ 带 Bearer 认证的 WebSocket
          ▼
宿主机独立 Codex app-server
          └── 本机项目、Codex 登录、工具和 skills
```

飞书应用的凭证只从本项目配置读取，不访问或更改 `lark-cli` 的配置。
每个「聊天 + 发送人」对应独立 Codex 会话，不接管桌面端已打开的对话。
Python 和 PostgreSQL 各一个容器；Codex 在宿主机执行。

## 已实现

- 多轮文本对话、会话持久化、同会话任务排队和重启恢复。
- 飞书进度卡、完整结果分段发送、发送失败后 `/last` 补取。
- 操作确认卡和文本确认；问题回答；停止任务。
- 新应用用户配对、消息去重、审批一次性使用及操作者校验。
- PostgreSQL advisory lock 防止两个桥接同时消费同一份队列。
- 控制命令也持久入队；重启后保留未处理命令，对执行状态未知的命令提示重新发送。
- 执行状态未知时暂停会话，避免重启后重复操作项目。
- 飞书连接状态/心跳监测、容器健康检查、异常退出自动重启。

当前输入支持文本。图片、文件、语音消息暂不处理。群聊默认关闭。
Codex 命令/文件审批和普通问题已接入；尚未接入的扩展权限请求会拒绝，不会默认放行。

## 1. 准备配置

宿主机需要 Docker Desktop、Docker Compose、Python 3.9+（启动脚本只用标准库）和已登录的 Codex CLI。
本项目已按 `codex-cli 0.163.0` 核对协议。app-server 的 WebSocket 接口仍为实验性接口，升级 Codex 后应先验证连接和协议。

```bash
bash scripts/bootstrap.sh
```

此命令只创建不存在的文件，不覆盖原有配置：

- `.env`：App ID、宿主机工作目录、端口等普通配置。
- `.secrets/feishu_app_secret`：填写**新飞书应用**的 App Secret，单行纯文本。
- `.secrets/codex_token`：自动生成的宿主机连接认证令牌。
- `.secrets/postgres_password`：自动生成的数据库密码。

`.env`、`.secrets` 都不会进入 Git 或 Docker 构建上下文。
`.secrets` 目录权限为 0700，文件为 0600；容器以只读 secret 挂载并将运行时副本放在私有 tmpfs。

`.env` 至少填写：

```dotenv
FEISHU_APP_ID=你的新应用AppID
CODEX_WORKSPACE=/宿主机上的项目绝对路径
CODEX_PORT=4500
```

工作目录使用**宿主机路径**，无需挂载项目到 Python 容器。
Codex 会继承宿主机登录和配置；桥接为自己的线程指定 `workspace-write`、按需确认、由用户处理审批，不修改全局配置。

## 2. 配置飞书新应用

在新企业自建应用的开发者后台操作：

1. 开启「机器人」能力。
2. 为应用身份申请 `im:message:send_as_bot`、`im:message.p2p_msg:readonly`。
3. 在「事件配置」中选择**长连接**，添加 `im.message.receive_v1`。
4. 在「回调配置」中选择**长连接**，添加 `card.action.trigger`。
5. 发布应用版本，并把自己加入应用可用范围。

长连接不需要公网域名或端口映射。后台可能要求先建立长连接才能保存配置；完成凭证填写并启动下面的 `pair` 后，可回到后台完成保存和发布。

如需群聊，再申请 `im:message.group_at_msg:readonly`，设置 `FEISHU_ALLOW_GROUPS=true`、`FEISHU_BOT_OPEN_ID=新应用机器人自己的open_id`，并把机器人加入群。仅授权用户 @本机器人的消息会被处理；机器人身份未配置时不启用群聊。机器人 open_id 可通过飞书开发者后台 API 调试台的「获取机器人信息」接口查询。

参考：[接收消息](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)、[卡片回传](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-callback-communication)、[官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)。

## 3. 启动宿主机 Codex 接口

在宿主机终端运行并保持它运行：

```bash
bash scripts/codex-host.sh
```

它启动独立的 `codex app-server`，默认监听 `127.0.0.1:4500`，要求 `.secrets/codex_token` 中的认证令牌。容器通过 Docker Desktop 的 `host.docker.internal` 连接它。
它不会连接、停止或修改桌面 Codex 的后台服务。

启动脚本会读取项目 `.env` 中的 `CODEX_PORT` 和 `CODEX_PROXY_URL`，环境变量优先。
若本机 Codex 需要代理访问模型服务，填写本机代理的 HTTP / mixed 监听地址，例如 Clash：

```dotenv
CODEX_PROXY_URL=http://127.0.0.1:7890
```

它给宿主机 Codex 设置大小写 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`，并将本机地址和 `host.docker.internal` 加入 `NO_PROXY`，保留已有绕过规则。
留空时继承终端现有代理；只读取配置值，不执行 `.env` 中的 shell 表达式。
修改代理后需停止并重新运行此宿主机脚本；Docker 容器无需配置这个本机代理地址。Clash 也需要保持运行。

如修改端口，在 `.env` 中改 `CODEX_PORT` 后重建 bridge 容器，并重新启动宿主机脚本。也可临时覆盖宿主机端口：

```bash
CODEX_PORT=4501 bash scripts/codex-host.sh
```

电脑需要保持开机和联网。若需要随登录自动启动，可以用 macOS 的服务管理器管理此脚本；本项目不会自动安装用户级系统服务。

参考：[Codex app-server](https://learn.chatgpt.com/docs/app-server)。

## 4. 启动双容器并配对

```bash
docker compose up -d --build
docker compose exec bridge python -m octopus check
docker compose exec bridge python -m octopus pair
```

`check` 验证配置、数据库连接和 Codex 认证握手，不执行模型任务。
`pair` 在终端显示一个十分钟有效的一次性配对码。把终端中的 `/pair ...` 命令私聊发给**新应用机器人**。

配对只在私聊生效，配对后的新应用用户 Open ID 保存到 PostgreSQL。
新应用和原 lark-cli 应用中的 Open ID 不应混用。
服务会在配对完成后自动开始接收任务；首次尚未配对时，容器会保持运行并显示 `waiting_pair`，健康检查暂不通过。

缺少 App Secret 时，服务处于 `waiting_configuration`，不会接收任务。
secret 文件在容器启动时复制，因此填写或修改凭证后执行：

```bash
docker compose up -d --force-recreate bridge
```

## 使用

直接发文本即可创建或继续当前 Codex 会话。任务执行中发来的新消息会排队；默认全局只执行一个任务，避免同一项目被并发修改。

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看命令 |
| `/status` | 查看当前会话、进度与排队数 |
| `/new` | 空闲时开始新会话 |
| `/stop` | 停止任务，取消本会话排队消息 |
| `/resume` | 检查中断任务后，解除会话暂停 |
| `/last` | 重新发送最近一次执行结果 |
| `/approve 编号` | 允许本次操作，也可点击卡片按钮 |
| `/deny 编号` | 拒绝本次操作 |
| `/answer 编号 答案` | 回答单个问题 |

多个问题请按照卡片提示，用 JSON 回答每个问题 ID。确认默认十五分钟过期；超时拒绝操作，提问超时则停止任务。
配对用户只能操作自己的会话和审批卡，旧编号不能重用。

如果服务或宿主机连接中断，执行中的任务会标记为结果待检查，所属会话暂停；已有排队消息会保留，不会立即重跑。
先检查宿主机项目，必要时 `/stop` 停止仍在运行的任务，再 `/resume` 恢复。
恢复会话时如果 Codex 仍有活跃任务，桥接会拒绝提交新任务，避免把它意外追加到原任务。

## 运维

```bash
docker compose ps
docker compose logs --tail=100 bridge
docker compose logs --tail=100 db
docker compose restart bridge
docker compose down
```

`docker compose down` 保留 PostgreSQL 持久卷；`down -v` 会删除会话、配对用户和任务数据。
PostgreSQL 不映射宿主机端口，只能通过 Compose 内部网络访问。
数据库中的一个 schema 绑定一个 App ID；更换应用时使用独立数据库或独立 schema，不混用用户身份。

备份：

```bash
mkdir -p .data
docker compose exec -T db pg_dump -U octopus octopus > .data/octopus-backup.sql
```

健康检查通过表示调度进程运行且飞书连接有近期心跳。首次权限和事件配置仍需通过真实私聊验证。

## 开发与验证

```bash
uv sync --frozen --python 3.12
uv run python -m unittest discover -s tests -v
docker compose exec bridge python -m octopus test-db
```

本地单元测试使用假 Codex 服务和模拟飞书接口；WebSocket 测试使用本地临时端口。
真实数据库测试只创建和清理随机 schema，不操作应用 schema。未设置 `OCTOPUS_TEST_DATABASE_URL` 时，本地数据库测试会跳过；容器中的 `test-db` 会启用真实 PostgreSQL 测试。

Python 依赖由 `uv.lock` 锁定，镜像使用含哈希的 `requirements.lock` 安装同一套依赖。

```text
src/octopus/config.py       配置与凭证文件读取
src/octopus/feishu.py       官方 SDK、长连接子进程、消息与卡片
src/octopus/codex.py        认证 WebSocket / stdio、JSON-RPC、线程生命周期
src/octopus/store.py        PostgreSQL 会话、队列、配对用户、结果和服务锁
src/octopus/bridge.py       调度、指令、审批、问题、恢复策略
src/octopus/__main__.py     启动、配对、健康检查
```

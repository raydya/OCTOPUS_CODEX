# OCTOPUS CODEX

使用一个独立的飞书企业自建应用，持续与宿主机上的只读 Codex 对话，回答业务和排障问题。

```text
飞书私聊（成员名单）
          │ 官方 Python SDK 长连接
          ▼
本项目 Docker Compose
  bridge：Python 3.12 ───── local-infra：共享 PostgreSQL 17
                              └── 独立数据库 octopus、独立账号
          │ 带 Bearer 认证的 WebSocket
          ▼
宿主机独立 Codex app-server
          └── 动态查询工具 → 宿主机认证查询接口（127.0.0.1:4501）
                              ├── TLS / Grafana / 飞书文档（现有查询账号）
                              └── ~/Projects/rere（受限源码读取）
```

机器人应用凭证只从本项目读取；文档查询复用宿主机 `lark-cli` 的现有身份。
每个「聊天 + 发送人」对应独立 Codex 会话，不接管桌面端已打开的对话。
Python 在本项目容器运行，PostgreSQL 由独立 `local-infra` Compose 管理；Codex 在宿主机执行。

## 已实现

- 多轮文本对话、会话持久化、同会话任务排队和重启恢复。
- 原消息「稍等」表情回应、完整结果分段发送、发送失败后 `/last` 补取。
- 只读工具白名单、问题回答、停止任务；禁止命令/文件/扩展权限审批升级。
- 管理员/成员启停、按用户的任务预算、审计和独立数据库运行账号。
- 新应用用户配对、消息去重、审批一次性使用及操作者校验。
- PostgreSQL advisory lock 防止两个桥接同时消费同一份队列。
- 控制命令也持久入队；重启后保留未处理命令，对执行状态未知的命令提示重新发送。
- 执行状态未知时暂停会话，避免重启后重复操作项目。
- 飞书连接状态/心跳监测、容器健康检查、异常退出自动重启。

当前支持文本私聊。共同查询模式禁止群聊，避免向未授权群成员公开结果。图片、文件、语音暂不处理。

## 1. 准备配置

宿主机需要 Docker Desktop、Docker Compose、Python 3.12+（宿主机脚本只用标准库）和已登录的 Codex CLI。
本项目已按 `codex-cli 0.163.0` 核对协议。app-server 的 WebSocket 接口仍为实验性接口，只读启动器锁定此已验证版本，升级后需重新验证工具注册与权限策略。

```bash
bash scripts/bootstrap.sh
```

此命令只创建不存在的文件，不覆盖原有配置：

- `.env`：App ID、宿主机工作目录、端口等普通配置。
- `.secrets/feishu_app_secret`：填写**新飞书应用**的 App Secret，单行纯文本。
- `.secrets/codex_token`：自动生成的宿主机连接认证令牌。
- `.secrets/query_token`：自动生成的查询接口认证令牌。
- `.secrets/postgres_owner_password`：本库所有者 `octopus_owner` 的迁移密码，仅维护容器使用；不是集群管理员。
- `.secrets/postgres_runtime_password`：bridge 的受限运行账号密码。

先由 `local-infra` 使用对应密码创建 `octopus` 数据库、`octopus_owner` 和 `octopus_runtime`，并创建内部网络 `local-infra-codex-db`。本项目 bootstrap 只准备本地文件，不创建数据库账号、不启动 PG。旧 `.secrets/postgres_password` 不再挂载或用于当前部署。

`.env`、`.secrets` 都不会进入 Git 或 Docker 构建上下文。
`.secrets` 目录权限为 0700，文件为 0600；容器以只读 secret 挂载并将运行时副本放在私有 tmpfs。

`.env` 至少填写：

```dotenv
FEISHU_APP_ID=你的新应用AppID
CODEX_WORKSPACE=/本项目绝对路径/.data/codex-readonly/workspace
CODEX_PORT=4500
CODEX_READ_ONLY=true
CODEX_DIAGNOSTICS_ENABLED=false
```

工作目录必须是本项目 `.data/codex-readonly/workspace` 的绝对路径，且保持空目录。启动器创建独立配置目录，仅通过单文件符号链接复用 Codex 登录；不继承个人 MCP、skills、hooks 或项目指令。
代码通过受控读取器访问 `~/Projects/rere`，不挂载到容器或暴露给模型内置文件工具。

`CODEX_MODEL` 留空时使用 bot 独立 Codex 配置的默认模型。`CODEX_REASONING_EFFORT=xhigh` 设置思考强度，Docker Compose 默认使用 `xhigh`。修改后重建 bridge 容器即可；桥接每轮都显式传入思考强度，因此已有会话从下一轮开始生效。

## 2. 配置飞书新应用

在新企业自建应用的开发者后台操作：

1. 开启「机器人」能力。
2. 为应用身份申请 `im:message:send_as_bot`、`im:message.p2p_msg:readonly`、`im:message.reactions:write_only`。
3. 在「事件配置」中选择**长连接**，添加 `im.message.receive_v1`。
4. 在「回调配置」中选择**长连接**，添加 `card.action.trigger`。
5. 发布应用版本，并把自己加入应用可用范围。

长连接不需要公网域名或端口映射。后台可能要求先建立长连接才能保存配置；完成凭证填写并启动下面的 `pair` 后，可回到后台完成保存和发布。

开放给同事时，先在应用后台扩大可用范围；同事能私聊 bot 后，可发送“申请权限”或 `/apply` 申请加入。bot 会用本应用身份给当前启用的管理员发送“通过 / 拒绝”卡片，管理员同意后成为普通成员。也可继续在 bot 成员名单手动添加。此流程只管理 bot 对话权限，飞书后台的应用可用范围仍需单独配置。
参考：[接收消息](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)、[卡片回传](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-callback-communication)、[官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)。

## 3. 启动宿主机 Codex 接口

在宿主机终端运行并保持它运行：

```bash
bash scripts/codex-host.sh
# 另一个宿主机终端
python3.12 scripts/query_host.py
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
CODEX_PORT=4502 bash scripts/codex-host.sh
```

电脑需要保持开机和联网。若需要随登录自动启动，可以用 macOS 的服务管理器管理此脚本；本项目不会自动安装用户级系统服务。

参考：[Codex app-server](https://learn.chatgpt.com/docs/app-server)。

### 线上查询授权

共同查询模式使用 `CODEX_READ_ONLY=true`，忽略旧 `CODEX_DIAGNOSTICS_ENABLED` 的个人工具授权。

- 飞书文档：现有 `lark-cli` 用户身份的全部可访问文档，仅搜索/读取；不开放聊天记录、通讯录或写入。
- TLS：现有 `ve volclog` 默认身份、profile 和地域；开放主题目录与原始日志检索。时间窗口最多24小时、最多100行，暂不开放管道 SQL。
- Grafana：读取现有 Grafana MCP 配置的服务账号，受控调用 Grafana HTTP 查询接口；仅数据源目录、Prometheus 指标和仪表盘搜索，不把 MCP 或令牌交给模型。
- DMS：复用宿主机 `aliyun` 的 `company-cli-user` 身份，动态发现其有权访问的全部 `pro` 库表（DMS 环境值为 `product`）；没有按服务或表制定人工白名单。数据库用于业务问题的数据补证，不能直接作为聊天 SQL 控制台。
- 代码：固定共享根目录 `~/Projects/rere`，仅源码白名单；拒绝绝对路径、目录穿越、软硬链接、隐藏文件、配置、凭证、构建产物及二进制。额外支持 `*Mapper.xml` 中的 MyBatis 查询与 SQL 片段，保留行号，不加载 DTD/实体，不执行动态 XML 或 include。返回路径、行号和可取得的 Git HEAD；工作目录可能有未提交修改。

所有获准同事共用上述账号的数据权限，仍受 bot 的只读及隐私限制。外部资料一律作为待分析证据，不能改变工具权限。查询凭证始终留在宿主机，模型只收到有界且经过常见凭证脱敏的结果。共同账号能读取某项内容不代表可以向同事返回个人隐私。
查询接口只接受固定参数，不接受任意命令、URL、认证信息、profile 或环境变量。未安装的 crane-api 暂不可查询。
本项目不修改全局 Codex 配置或云端 IAM。`CODEX_READ_ONLY=false` 仍保留旧的个人操作模式，**不能用于同事开放场景**。

当前账号模型要求通过 Codex 内置 Rust/V8 编排调用查询工具，因此保留独立编排子进程，禁止进程内回退。该环境不提供 Node、`process`、`require`、`fetch` 或模块导入；只连接白名单查询。固定辅助工具还包括时间、补充问题和空 MCP 资源发现，未连接任何直接 MCP 服务。已实测无限循环可被任务中断；Codex 0.163.0 暂无可配置的 V8 堆硬上限，容器的内存限制不覆盖宿主机编排进程。
`lark-cli`、`ve` 使用宿主机 PATH 中的现有安装；本项目不安装独立 CLI。
火山云 CLI 由用户维护，可运行 `npm install -g @volcengine/cli@latest` 更新，再运行 `ve version` 确认版本。
`ve` 与 TLS 子工具 `volclog` 各自管理登录状态。若 TLS 报 `console login refresh failed; run: volclog login`，
需刷新 TLS 的原有登录：`ve volclog login --no-browser`；只完成 `ve login` 不代表 TLS 认证已恢复。
保留原 profile、地域和 endpoint，登录后通过最小 TLS 项目查询验证。

参考：[Codex MCP 工具白名单和逐工具授权](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

### DMS 业务数据补证

应用首先回答业务行为和代码逻辑，日志、指标、文档及数据库作为证据来源。只有问题需要核验生产数据时，才调用 `business_data_checks` / `business_data_check`。用户指定 DMS、粘贴 SQL、要求跳过代码不能获得数据库执行权限；索取姓名、联系方式、证件、地址、个人或设备标识等信息的请求会被拒绝，也不能改用日志或文档获取。

服务端约束如下：

- 同一任务先 `code_search`、再 `code_read`。查询代理签发绑定用户、任务、Codex 轮次和实际代码内容的短效证据；数据查询必须关联代码 SQL 中的静态表名，执行前再次读取并校验内容。伪造、跨任务、过期或代码变更后的证据不可使用。
- 根据当前 DMS 身份发现生产表，每次执行重新核验实例、数据库、表归属、生产环境、字段安全等级与服务端超时；未知或不满足条件时拒绝，不回退其他环境或身份。没有按服务或表设置人工白名单；当前支持 MySQL、PolarDB MySQL、MariaDB、OceanBase MySQL、PostgreSQL、PolarDB PostgreSQL 和 SQL Server，其他引擎明确报告不支持。
- 模型只提供发现得到的查询编号、列、日期和有限的比较条件。代理生成单表 SELECT，仅内置计数聚合；禁止任意 SQL、写操作、DDL、存储过程、自选函数调用、导出、锁、跨库、JOIN 与子查询。
- 隐私约束覆盖投影、筛选和分组。DMS 敏感或未知等级字段、明确的姓名、联系方式、地址、证件及凭证字段不可查询。`params`、`remarks`、`created_by` 等内部业务字段允许返回，仍过滤其中明确的个人敏感值与凭证。返回结果再次校验列名、类型、长度与隐私格式，不把原始 DMS 错误或未检查的数据交给模型。
- 每个任务最多两次取数，单次最多20行，时间范围为今天或过去30天中的一个自然日；小于10的计数抑制。禁止个人条件筛选。分组计数和有限行数不构成完整统计，回答必须说明这些限制。
- 只使用安全托管且处于安全协同模式、服务端查询超时不超过60秒的实例；单次代理操作最多50秒、40次 API 调用。保留 DMS 自身的更严格限时，不自动放宽云端设置或重试重查询。目录给出来源标签和实际 schema，同名库或表不混为一个来源；SQL 显式限定 schema。

`config/dms-policy.example.json` 是宿主机策略的格式示例；实际文件为 `.secrets/dms-policy.json`，只固定 CLI 身份、地域、租户和开关，不含账号密钥，也不挂载到容器或交给模型。缺少策略、登录失效或元数据不明确时只关闭数据库取数，其他查询继续工作。OAuth 失效时在本机运行：

```bash
aliyun configure --mode OAuth --oauth-site-type CN --profile company-cli-user
```

隐私意图拦截还包括 Unicode 规范化、任务入队和工具调用的再次检查；`/answer` 只能收紧本轮数据库权限。词法识别不能证明模型理解了业务必要性，也不能识别所有隐晦、跨轮次的隐私请求；DMS 字段分类可能不完整，小群体抑制也不是匿名性证明。允许的业务文本及现有日志/文档会过滤明确格式的个人信息与凭证，但不能保证识别自由文本中所有个人敏感信息。

本次策略使用 `readonly:v2` 会话命名空间。首次切换后创建新的 Codex 会话，旧对话及结果保留，但不复用旧线程的工具与指令。

接口依据：[DMS ExecuteScript](https://help.aliyun.com/zh/dms/developer-reference/api-dms-enterprise-2018-11-01-executescript)、[生产环境标识](https://help.aliyun.com/zh/dms/developer-reference/api-dms-enterprise-2018-11-01-listinstances)、[字段敏感等级](https://help.aliyun.com/zh/dms/developer-reference/api-dms-enterprise-2018-11-01-listcolumns)。

## 4. 连接共享数据库并配对

```bash
docker compose build bridge maintenance
docker compose run --rm maintenance
docker compose up -d bridge
docker compose exec bridge python -m octopus check
docker compose exec bridge python -m octopus pair
```

运行前先在 `local-infra` 启动共享 PG。本项目通过外部网络 `local-infra-codex-db` 上的 `shared-postgres:5432` 连接 `octopus` 数据库，无本地 `db` 服务或 PG 数据卷。
维护命令使用 `octopus_owner` 迁移表结构、授权已由基础设施创建的 `octopus_runtime`，不创建/修改角色或读取运行账号密码。owner 必须是本库所有者且没有集群管理权限或其他角色成员资格；runtime 必须是专用 `NOINHERIT LOGIN`，没有管理权限、角色成员资格或对象所有权。首次安装和升级都先执行维护命令。
bridge 使用 runtime，无 DDL/角色管理/审计修改权限。数据库连接最多等待 30 秒；等待期结束后启动失败，由 Compose 的重启策略重试，不自动重试 SQL 或任务。

迁移保留 `octopus.meta` 中的 `app_id`、`runtime_role` 和全部业务记录；不同 App ID 或已绑定的不同 runtime 会拒绝维护。共享实例中其他应用必须使用独立数据库和账号；数据库 CONNECT 与跨库访问限制由 `local-infra` 管理。
`check` 验证数据库、Codex 认证与只读策略、查询接口健康，不执行模型任务或向飞书发消息。
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

直接发文本即可创建或继续当前 Codex 会话。任务执行中发来的新消息会排队；默认全局只执行一个任务。每人最多3个运行或排队任务、过去24小时100次、任务间隔2秒、单任务最多30次查询和600秒。参数见 `.env.example`。

收到任务消息后，机器人先在原消息上添加「稍等」（`OneSecond`）表情，再开始执行；排队消息也会先收到表情回应。
执行期间不发送进度卡，使用 `/status` 查看当前进度和排队数。结束时移除表情，直接回复回答正文，不显示“Codex 已完成”等结果标题；`/last` 和长回答分段也只显示正文。补充信息和错误仍会发消息提示。查询和最终输出均有大小上限。
表情 ID 保存在 PostgreSQL，取消排队或重启后也会清理已结束任务的表情。若表情接口暂时失败，任务仍会继续；排查容器日志及新应用的 `im:message.reactions:write_only` 权限。
从旧版升级时，需在飞书后台为新应用补充此权限并发布版本。表情使用机器人的应用身份，和本机 `lark-cli` 查询身份无关。

接口参考：[添加消息表情回复](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message-reaction/create)、[飞书表情列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message-reaction/emojis-introduce)。

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看命令 |
| `/status` | 查看当前会话、进度与排队数 |
| `/new` | 空闲时开始新会话 |
| `/stop` | 停止任务，取消本会话排队消息 |
| `/resume` | 检查中断任务后，解除会话暂停 |
| `/last` | 重新发送最近一次执行结果 |
| `/members` | 管理员私聊列出成员 |
| `/member-add ou_…` | 管理员添加普通成员；不会自动恢复已停用成员 |
| `/member-disable ou_…` | 管理员停用成员，取消其排队并停止运行任务 |
| `/member-enable ou_…` | 管理员恢复成员 |
| `/apply` 或“申请权限” | 未授权用户在私聊中申请对话权限 |
| `/access-requests` | 管理员私聊查看待处理的权限申请 |
| `/access-approve 申请编号` | 管理员通过申请；通常直接点击通知卡片即可 |
| `/access-deny 申请编号` | 管理员拒绝申请 |
| `/access-test` | 管理员给自己发送测试申请卡片，验证通知和按钮；不改变成员权限 |
| `/answer 编号 答案` | 回答单个问题 |

多个问题请按照卡片提示，用 JSON 回答每个问题 ID。问题默认十五分钟过期，超时停止任务。只读模式一律拒绝命令/文件/扩展权限请求。
配对用户只能操作自己的会话和审批卡，旧编号不能重用。

如果服务或宿主机连接中断，执行中的任务会标记为结果待检查，所属会话暂停；已有排队消息会保留，不会立即重跑。
先检查上次任务状态，必要时 `/stop` 停止仍在运行的任务，再 `/resume` 恢复。
恢复会话时如果 Codex 仍有活跃任务，桥接会拒绝提交新任务，避免把它意外追加到原任务。

本地成员管理（使用新机器人应用的 open_id）：

```bash
docker compose exec bridge python -m octopus members list
docker compose exec bridge python -m octopus members add ou_同事的ID
docker compose exec bridge python -m octopus members disable ou_同事的ID
# 新增管理员仅允许可信本地操作，聊天中不能提升角色
docker compose exec bridge python -m octopus members add ou_管理员ID --admin
```

旧配对用户只迁移一次为管理员，重启不会重新启用已停用用户。停用会取消队列，每次工具调用和结果发出前重新检查授权；运行任务还由2秒巡检请求停止。已经发出的飞书消息不会撤回。

### 同事申请对话权限

未授权同事发送普通消息时，只会收到申请说明，不会创建 Codex 会话或执行查询。发送“申请权限”或 `/apply` 后，申请和通知任务写入 PostgreSQL；管理员收到的卡片包含申请人、申请编号和授权范围。点击“通过”或“拒绝”后，bot 会通知申请人并关闭审批按钮。通过后请同事重新发送问题，之前未授权的消息不会自动执行。

管理员已有权限时，可发送 `/access-test` 给自己生成一张标明“测试”的申请卡片，分别验证通过、拒绝和结果通知。此命令不新增真实申请，也不修改任何成员权限；测试卡片10分钟内有效，服务重启后需重新发送命令。未授权同事无需事先知道申请命令，第一次直接提问会收到“发送‘申请权限’或 /apply”的引导；短时间重复消息只提醒一次。已停用成员会收到联系管理员的提示。

每人同时只保留一个待审批申请，有效期7天；重复申请不重复通知管理员。拒绝后24小时可重新申请，已停用成员须由管理员通过 `/member-enable` 恢复，不能靠申请或旧卡片绕过停用。服务会核验当前管理员身份、实际发送给该管理员的卡片 ID、申请状态，并在同一事务中写入决定、普通成员授权和审计记录。成员资格不会授予管理员角色。

待审批记录、通知状态和结果在重启后保留。通知失败会重试，发送使用固定 UUID 并记录返回的消息 ID；平台去重有时间窗口，不能保证极端故障下完全不重复通知，但审批决定只生效一次。管理员可以用 `/access-requests` 和审批命令处理尚未收到卡片的申请。当前启用的管理员都会收到申请通知，使用的是此 bot 的应用身份和 open_id，不使用本机 lark-cli 身份。

升级此功能时，先构建并运行 maintenance 迁移至 schema v3、更新专用运行账号的表权限，再重建 bridge。权限仍使用已有的 `im:message:send_as_bot` 和长连接 `card.action.trigger`；不需要新增用户 OAuth 或通讯录读取授权。
只读模式使用新的会话命名空间，不恢复旧个人模式线程，也不执行其遗留排队任务。
每人最多5个待执行/运行控制命令、过去24小时最多1000条；普通命令最多512字，补充答案最多4000字。

## 运维

```bash
docker compose ps
docker compose logs --tail=100 bridge
docker compose restart bridge
docker compose down
```

本项目 `docker compose down` 只关闭应用，不关闭共享 PG，也不删除基础设施持久卷。PG 的启停、日志、升级、备份由 `local-infra` 管理；其维护窗口会影响使用该实例的应用。
bridge 通过专用内部网络连接 PG；不挂载其他应用或数据库管理员凭证。基础设施可以为宿主机应用开放仅绑定 loopback 的 PG 端口。
数据库中的 `octopus` schema 绑定一个 App ID；增加其他应用时使用独立数据库和账号，不混用用户身份。

维护账号可清理30天前的终态任务/结果、90天前的审计（请按运维计划定期执行；当前未安装定时任务）：

```bash
docker compose run --rm maintenance /app/scripts/db_roles.py --retention
```

审计只记录操作者、任务 ID、操作类型和允许/拒绝结果，不保存查询正文。任务/结果保存在 PG，模型会话另存于宿主机隔离的 CODEX_HOME；PG 清理不等于清理 Codex 会话或飞书消息。
bridge 使用只读根文件系统、非 root 业务进程、独立 tmpfs、资源上限。宿主机隔离目前是已核验的工具和文件策略，**不是独立 macOS 用户或虚拟机**，宿主机管理员仍属信任边界。

数据库备份和恢复由 `local-infra` 按库管理，恢复时保留 `octopus` schema、App ID/运行账号绑定和迁移版本。数据对象应归 `octopus_owner` 所有，再执行本项目 maintenance 重建最小权限；不能把旧集群超级用户授权带入共享实例。
完整应用恢复还需备份宿主机的隔离 `CODEX_HOME`；凭证应单独安全保存，不能写入备份日志或 Git。旧 PG 数据卷及迁移回退副本已清理；数据库恢复使用 `local-infra` 的日常备份。

健康检查通过表示调度进程运行且飞书连接有近期心跳。首次权限和事件配置仍需通过真实私聊验证。

## 开发与验证

```bash
uv sync --frozen --python 3.12
uv run python -m unittest discover -s tests -v
```

本地单元测试使用假 Codex 服务和模拟飞书接口；WebSocket 测试使用本地临时端口。
数据库集成测试仅连接独立的临时测试 PG 实例，通过 `OCTOPUS_TEST_DATABASE_URL` 提供测试管理员连接；不要使用正式共享 PG 或 maintenance 凭证。测试使用随机 schema/数据库/角色，完成后删除，验证迁移 owner 无角色管理权限、runtime 无 DDL 和审计修改权限。未设置测试连接时跳过数据库测试。

Python 依赖由 `uv.lock` 锁定，镜像使用含哈希的 `requirements.lock` 安装同一套依赖。

```text
src/octopus/config.py       配置与凭证文件读取
src/octopus/feishu.py       官方 SDK、长连接子进程、消息与卡片
src/octopus/codex.py        认证 WebSocket / stdio、JSON-RPC、线程生命周期
src/octopus/store.py        PostgreSQL 会话、队列、配对用户、结果和服务锁
src/octopus/bridge.py       调度、指令、审批、问题、恢复策略
src/octopus/__main__.py     启动、配对、健康检查
```

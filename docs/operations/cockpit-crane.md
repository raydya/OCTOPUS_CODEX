# Crane 线上状态查询：使用现有 Cockpit CLI

## 名称与入口

用户所说的 `crane-api` 是 **Cockpit CLI 提供的 Crane 工程平台查询能力**，不是需要另行安装的 `crane-api` 可执行文件或 MCP 服务。排障时先发现 PATH 中的 `cockpit`，复用现有安装和登录身份。

```bash
command -v cockpit
cockpit version
cockpit --help
```

通过当前 CLI 的 `--help` 确认命令和参数，不固定版本、不另装 CLI。缺少同名 MCP 或 `crane-api` 命令不能证明 Crane 查询不可用。

## 查询顺序

1. 用集群目录发现真实的 cluster slug 和环境；用应用目录确认应用名。
2. 按目标应用、生产集群和 namespace 收窄查询，先查 workload，再查 Pod。名称取自实际返回结果，不根据 TLS 项目名推算集群。
3. 需要进一步解释状态时，再对列表返回的 workload/Pod 名称调用 `describe`。其他能力先查相应子命令的帮助。

以下是已验证的命令示例；`prod-jd-cloud`、`java-service` 和应用名仅是当时查询的目标，其他问题应重新定位：

```bash
cockpit cluster list --no-color -o json
cockpit app list --all --app opt-inspection-service --no-color -o json
cockpit service workload list --cluster prod-jd-cloud --namespace java-service --app opt-inspection-service --no-color -o json
cockpit service pod list --cluster prod-jd-cloud --namespace java-service --app opt-inspection-service --no-color -o json
cockpit service workload describe --help
cockpit service pod describe --help
cockpit deploy release --help
```

`service` 还提供 canary、gray、web 子命令；`deploy` 提供构建和发布相关能力。这些命令组同时包含读写操作，不能因命令组可用就视为所有子命令均可执行。

## 读取与诊断边界

- 默认执行 `list`、`describe` 等只读查询。发布、重启、回滚、删除、切流、扩缩容及进入容器等操作，必须依据用户对具体动作的授权；查询授权不包含这些动作。
- CLI 自行使用已有认证，不读取或输出 token。认证或权限失败时说明具体限制，并在对话中请求必要授权；不要求用户粘贴凭证到聊天记录。
- JSON 列表字段可能使用大写开头，如集群的 `Env`、`Slug`、`Status`，workload 的 `Name`、`Ready`、`Available`，Pod 的 `Status`、`RestartCount`。以实际输出为准。
- 退出码为 0 时也可能只返回 `{"message": ...}`，必须核对响应结构及消息含义；不能直接当作成功的资源列表或证明没有资源。
- `describe` 可能返回完整 YAML，包含环境变量等敏感值。只提取诊断所需的状态、副本、重启、版本、探针等字段，不原样输出完整配置。
- TLS 用于原始日志，Grafana 用于指标和告警，Cockpit/Crane 用于服务实例、部署及发布状态；结合相同服务、环境和时间范围交叉验证。当前 Pod 状态不能直接证明历史告警期间的状态，也不能单独确定业务锁持有者。
- 通过 Cockpit 的文档和帮助确认 Crane 接口；不猜测 Grafana 数据源代理就是 Crane 的访问入口。此前 Grafana 代理请求的 502 只证明那次代理请求失败，不能证明 Cockpit/Crane 不可用。

## 验证记录与本项目接入状态

2026-11-13，本机 PATH 中的 `cockpit 1.2.0` 使用已有登录身份成功查询集群、应用、workload 和 Pod。当时 `opt-inspection-service` 的结果为：

| 集群 | 就绪副本 | 当前 Pod 状态 | 当前 Pod 重启次数 |
| --- | --- | --- | --- |
| prod-jd-cloud | 16/16 | 16 个均为 Running | 均为 0 |
| prod-hk | 1/1 | 1 个为 Running | 0 |

这是验证时的快照，不是实时状态或后续排障结论。验证没有执行生产变更，也没有重新安装或登录 CLI。

截至上述验证，OCTOPUS_CODEX 尚未把 Cockpit 接入 `queries.py` / `query_host.py` 的受控查询工具。宿主机可查询不等于飞书机器人已能查询；机器人原生 shell 的网络权限关闭，增加指引本身不会赋予联网能力。现有对话授权卡片仅覆盖受控 TLS 查询，尚不覆盖 Cockpit。正式接入应复用现有 CLI，以固定参数开放必要的只读操作，并核验恢复的历史会话是否获得新工具。

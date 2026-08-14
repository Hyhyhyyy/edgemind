# EdgeMind 架构与云边通信机制详解

## 1. 组件全景

```mermaid
flowchart TB
    subgraph Cloud["云端（K8s）"]
        VLLM[vLLM 大模型<br/>OpenAI 兼容]
        GW[vLLM Gateway<br/>推理网关]
        CTRL[Controller<br/>节点/事件控制]
        HUB[CloudHub<br/>WebSocket 网关]
        DIAG[Diagnoser<br/>LLM 自愈 Agent]
        API[HTTP 控制面<br/>+ 看板]
        GW --> VLLM
        CTRL --> GW
        DIAG --> CTRL
        DIAG --> GW
        HUB --> CTRL
        API --> CTRL
        API --> HUB
    end

    subgraph Edge["边缘节点（KubeEdge 纳管）"]
        EH[EdgeHub<br/>长连接客户端]
        EA[EdgeAgent<br/>心跳/执行指令]
        LI[LocalInference<br/>边端轻量模型]
        EH --> EA
        EA --> LI
    end

    HUB <-->|WebSocket 长连接| EH
    EA -.本地不可用路由.-> GW
```

## 2. 云边通信机制（对齐 KubeEdge）

KubeEdge 的真实链路：`edgecore/edgehub` ⇄ `cloudcore/cloudhub`，基于 WebSocket，
消息以 `operation + resource` 路由到 `edgeController` / `deviceController`。

本项目同构实现：

| KubeEdge | EdgeMind | 说明 |
|---|---|---|
| `cloudcore.cloudhub` | `CloudHub`（`cloud/cloudhub.py`） | 云端 WebSocket 入口，维护 `node_id → 连接` 路由表 |
| `edgecore.edgehub` | `EdgeHub`（`edge/edgehub.py`） | 边端长连接客户端，自动重连 |
| `EdgeController` | `Controller`（`cloud/controller.py`） | 节点注册/心跳/指标/事件/指令 |
| `resource/message` | `Envelope`（`common/protocol.py`） | `{type, node_id, payload, seq, ts}` 统一信封 |
| 设备孪生 / 状态上报 | `METRICS` / `EVENT` / `HEARTBEAT` | 边端周期上报 |

### 帧层（自研 `common/ws.py`）

- RFC6455 握手：`Sec-WebSocket-Key` → `Sec-WebSocket-Accept`；
- 掩码：客户端→服务端必须 mask（本项目修复过「长度字节未置掩码位」的坑）；
- 长度：7 / 16 / 64 位扩展；
- 心跳：ping/pong；断线：指数退避重连。

## 3. 一条消息的旅程（以心跳为例）

```mermaid
sequenceDiagram
    participant E as EdgeAgent
    participant EH as EdgeHub
    participant HUB as CloudHub
    participant C as Controller
    E->>EH: HEARTBEAT (每 HB_INTERVAL)
    EH->>HUB: WebSocket 帧(已掩码)
    HUB->>HUB: 更新 node_id→连接 路由
    HUB->>C: handle(Envelope)
    C->>C: 刷新 last_heartbeat, 相位→Running
    Note over C: tick() 检测超时 → heartbeat_lost
```

## 4. 自愈闭环

```mermaid
flowchart LR
    A[故障发生<br/>心跳丢失/GPU OOM/时延突增] --> B[检测<br/>tick / 指标异常]
    B --> C[RCA<br/>vLLM 根因分析]
    C --> D{解析成功?}
    D -- 是 --> E[结构化 JSON 决策]
    D -- 否 --> F[确定性规则兜底]
    E --> G[playbook 执行]
    F --> G
    G --> H[改状态/下发指令/模拟运维]
    H --> I[闭环标记已解决]
```

## 5. 云边协同推理路由

| 条件 | 路由决策 | 执行方 |
|---|---|---|
| 本地模型已加载且健康 | 边端本地推理（低时延/离线） | `LocalInference` |
| 本地不可用 / 过载 | 路由到云端 vLLM | `EdgeAgent.infer` → 云端 `/v1/chat/completions` |
| 云端 vLLM 排队高 | 扩容副本 + 下沉部分请求到边端 | `scale_cloud_replicas` / `route_to_local_edge` |

## 6. 故障类型与处置对照

| 故障 | 检测来源 | RCA 根因 | 修复动作 |
|---|---|---|---|
| 心跳丢失 | `tick()` 超时 | 云边长连接中断 | mark_not_ready + reschedule_to_cloud + alert_oncall |
| GPU OOM | 边端 `EVENT` / 指标 | 显存不足，本地模型被杀 | fallback_edge_to_small_model + route_inference_to_cloud + restart_edge_runtime |
| 模型加载失败 | 边端 `EVENT` | 权重拉取/加载失败 | route_inference_to_cloud + restart_edge_runtime + alert_oncall |
| 时延突增 | 指标异常 | 云端排队 / 云边 RTT 高 | scale_cloud_replicas + route_to_local_edge + throttle_low_priority |

# EdgeMind · 云边协同大模型推理与 LLM 自愈运维平台

> 一个能**完整体现 Python 工程能力、Linux/Docker/Kubernetes、KubeEdge 云边协同、
> vLLM 大模型推理、以及 LLM Agent 故障自愈**的创新项目。零第三方依赖即可运行，
> 并配套完整的容器化 / K8s / KubeEdge 落地方案。

---

## ⚠️ 项目性质说明（必读）

**这是作者（[@Hyhyhyyy](https://github.com/Hyhyhyyy)）个人对 AI Infra（AI 基础设施）领域的一次
了解与尝试（learning / practice project），并非生产级系统。**

项目目标是通过**亲手实现一个能跑、能演示的最小系统**，把以下方向串起来、建立体感：

- 云边协同架构与 KubeEdge 的云边通信机制；
- vLLM 等大模型推理服务的接入与云边分层调度；
- LLM Agent 在运维场景（故障根因分析 + 自动修复）中的落地；
- 配套的 Docker / Kubernetes / KubeEdge 工程化交付。

代码以**可读性、可运行、可演示**为先，许多组件（WebSocket、本地推理、LLM 推理）都做了
**零依赖的最小自研实现或 mock 降级**，便于在普通笔记本上直接跑通，而非依赖特定硬件/集群。
真实接入大模型只需设置 `VLLM_BASE_URL` 指向你的 vLLM 服务即可。

> 本仓库 **contributor 仅作者一人**。欢迎学习、参考、提 issue 交流，但请勿直接合入改动。

---

## 1. 项目定位与要解决的问题

边缘场景（工厂、车联网、门店、基站）普遍面临：

1. **算力分布不均**——云端有大模型（vLLM），边端只有小算力，但边端要求低时延、可离线；
2. **云边网络不可靠**——弱网/断电导致控制通道中断，节点「失联」；
3. **运维人力稀缺**——边端分散、故障类型多（GPU OOM、模型加载失败、时延突增、心跳丢失），
   靠人工排查成本高、响应慢。

**EdgeMind** 用一套「云边协同 + 大模型推理 + LLM 自愈 Agent」的组合拳解决上述问题：
- **云端**跑 vLLM 大模型 + 控制面；
- **边端**跑轻量本地推理 + 心跳/指标上报；
- **云边**通过一条 WebSocket 长连接（对齐 KubeEdge EdgeHub/CloudHub）协同；
- **LLM Agent** 持续巡检，对故障做根因分析（RCA）并自动执行修复剧本，闭环自愈。

---

## 2. 核心架构

```
                         ┌─────────────────────────── 云端 K8s ───────────────────────────┐
                         │                                                                 │
   ┌──────────────┐      │   ┌────────────┐   ┌──────────────┐   ┌──────────────────┐    │
   │  vLLM 大模型  │◄─────┤   │ vLLM Gateway│◄──┤  Controller  │◄──┤  Diagnoser Agent │    │
   │ (OpenAI 兼容) │      │   │ (推理网关)  │   │ (节点/事件)  │   │ (RCA + 自愈)     │    │
   └──────────────┘      │   └─────▲──────┘   └──────▲───────┘   └──────────────────┘    │
                         │         │                 │   ▲ 查询/指令                         │
                         │   ┌─────┴──────┐          │   └────────────────┐                │
                         │   │  CloudHub  │◄─── WebSocket 长连接 ────────┐ │                │
                         │   │ (云端网关) │                              │ │                │
                         │   └───────────┘                              │ │                │
                         └──────────────────────────────────────────────┼─┼────────────────┘
                                              ▲                          │ │
                                              │  心跳/指标/事件          │ │ 指令/路由
                                              │                          │ │
                         ┌────────────────────┴──────────────────────────┴─┴───────────────┐
                         │ 边缘节点（KubeEdge 纳管）                                        │
                         │   ┌──────────┐   ┌──────────────┐   ┌────────────────────┐      │
                         │   │ EdgeHub  │◄──┤  EdgeAgent   │◄──┤  LocalInference    │      │
                         │   │(长连接)  │   │(心跳/执行指令)│   │ (边端轻量模型)     │      │
                         │   └──────────┘   └──────────────┘   └────────────────────┘      │
                         └────────────────────────────────────────────────────────────────┘
```

更详细的组件交互与 KubeEdge 映射见 [docs/architecture.md](docs/architecture.md)。

---

## 3. 云边通信机制（对齐 KubeEdge）

本项目**手写了最小可用的 WebSocket 实现**（`src/common/ws.py`，RFC6455，纯标准库），
真实复现了 KubeEdge 中 EdgeHub ↔ CloudHub 的关键机制：

- **握手**：HTTP Upgrade 到 WebSocket，Sec-WebSocket-Accept 校验；
- **双向帧封装**：客户端→服务端必须掩码（mask），服务端→客户端不掩码；支持 7/16/64 位长度；
- **心跳保活**：ping/pong；
- **断线重连**：`WSClient` 按退避策略自动重连，连接断开不丢状态。

对应 KubeEdge 概念：`EdgeHub`=edgecore 的 edgehub，`CloudHub`=cloudcore 的 cloudhub，
消息通过 `operation + resource` 路由（本项目用 `Envelope.type` 实现）。

---

## 4. 云边协同推理

- 简单 / 离线请求由**边端本地轻量模型**（`LocalInference`）低时延处理；
- 复杂请求或本地不可用时，**自动路由到云端 vLLM**（`EdgeAgent.infer` → 云端 `/v1/chat/completions`）；
- 云端 vLLM 过载时，诊断 Agent 通过 `scale_cloud_replicas` / `route_to_local_edge` 做弹性调度。

---

## 5. LLM 自愈 Agent（故障排查经验落地）

诊断 Agent 每 `DIAG_INTERVAL` 秒一轮：

1. `controller.tick()` 检测心跳超时 → 生成 `heartbeat_lost`；
2. `_detect_anomalies()` 从指标识别时延/显存异常（去重，避免反复建单）；
3. 对每条故障调用 **vLLM 做 RCA**，要求返回结构化 JSON 决策（根因 + 动作白名单）；
4. 解析失败或 vLLM 不可用时，**回退到确定性规则**（体现真实排障经验）；
5. 把动作交给 **playbook** 执行（改状态 / 下发指令 / 模拟 kubectl 运维）；
6. 闭环标记事件已解决。

动作白名单（安全边界，模型只给建议、playbook 落地）：
`mark_not_ready / reschedule_to_cloud / cordon / fallback_edge_to_small_model /
route_inference_to_cloud / restart_edge_runtime / restart_edge_node /
scale_cloud_replicas / route_to_local_edge / throttle_low_priority / alert_oncall`

排障方法论知识库见 `src/agent/troubleshoot.py`（`python -m agent.troubleshoot --live` 可实时巡检）。

---

## 6. 技术栈

| 层 | 技术 |
|---|---|
| 语言 | **Python 3.12**（标准库实现，零第三方依赖即可运行） |
| 云边通信 | 自研 WebSocket（RFC6455）+ JSON 消息协议 |
| 推理服务 | **vLLM**（OpenAI 兼容接口），离线 mock 可降级 |
| 容器 / 编排 | **Docker** / **Docker Compose** / **Kubernetes**（Deployment + DaemonSet） |
| 边缘框架 | **KubeEdge**（CloudHub/EdgeHub、EdgeNode/Device CRD 对齐） |
| 智能体 | LLM（vLLM）RCA + 规则兜底 + 修复剧本（playbook） |

---

## 7. 快速开始

### 方式一：一键演示（零依赖，推荐先看这个）

```bash
cd edgemind
python demo/run_demo.py
# 浏览器打开 http://localhost:8000 看实时看板
# 另开终端：python -m agent.troubleshoot --live  做实时巡检
```

演示会自动注入三类故障并展示自愈：
- `edge-2` 心跳丢失 → 标记 NotReady + 任务迁云 + 告警
- `edge-1` GPU OOM → 降级小模型 + 路由到云 + 重启运行时
- `edge-1` 时延突增 → 云端扩容 + 下沉边端 + 限流

### 方式二：容器化

```bash
cd edgemind/deploy/docker
docker compose up --build
# 接入真实 vLLM：取消 docker-compose.yml 中 vllm 服务注释，并设置 cloud 的 VLLM_BASE_URL
```

### 方式三：Kubernetes / KubeEdge

```bash
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/cloud-deployment.yaml
kubectl apply -f deploy/k8s/edge-daemonset.yaml
# KubeEdge 集成见 deploy/kubeedge/README.md
```

### 测试

```bash
python tests/test_protocol.py
python tests/test_ws.py
python tests/test_diagnoser.py
```

---

## 8. 目录结构

```
edgemind/
├── src/
│   ├── common/   protocol.py(云边消息协议) ws.py(最小WebSocket) config.py logging_setup.py
│   ├── cloud/    cloudhub.py(云端网关) controller.py(节点控制) vllm_gateway.py(vLLM网关) api.py(控制面+看板) serve.py
│   ├── edge/     edgehub.py(边端客户端) edge_agent.py(边端智能体) local_inference.py(本地推理)
│   └── agent/    diagnoser.py(自愈Agent) playbook.py(修复剧本) prompts.py(提示词) troubleshoot.py(排障CLI)
├── demo/         run_demo.py(一键演示) scenarios.py(故障场景)
├── tests/        test_protocol.py test_ws.py test_diagnoser.py
└── deploy/
    ├── docker/   Dockerfile.cloud Dockerfile.edge docker-compose.yml
    ├── k8s/      configmap.yaml cloud-deployment.yaml edge-daemonset.yaml kubeedge-crds.yaml
    └── kubeedge/ cloudcore-values.yaml README.md
```

---

## 9. 五维能力对照（直接回应需求）

| 需求 | 在本项目中的体现 | 位置 |
|---|---|---|
| ① Python + 工程能力 | 全量 Python，类型注解/日志/配置/测试/模块化 | 全部 `src/`；`tests/` |
| ② Linux/Docker/K8s | 多阶段镜像、Compose、Deployment/DaemonSet/Service/HPA、探针 | `deploy/` |
| ③ KubeEdge 架构与云边通信 | 手写 EdgeHub/CloudHub 长连接 + 消息协议，对齐 KubeEdge 概念 | `ws.py` `cloudhub.py` `edgehub.py` `deploy/kubeedge/` |
| ④ vLLM 推理服务 | OpenAI 兼容网关 + 云边协同推理路由 + 离线 mock 降级 | `vllm_gateway.py` `local_inference.py` |
| ⑤ LLM Agent + 故障排查 | RCA 诊断 Agent + 规则兜底 + 修复剧本 + 排障知识库 | `diagnoser.py` `playbook.py` `troubleshoot.py` |

详细证据见 [docs/competency-map.md](docs/competency-map.md)。

---

## 10. 创新点

1. **把 LLM Agent 放在云边运维闭环里**：不是「告警给人」，而是「LLM 直接给出可执行的修复动作并自动落地」，且动作白名单化保证安全；
2. **云边分层推理 + 弹性路由**：本地小模型保时延/离线，云端大模型保能力，按需互备；
3. **零依赖可运行 + 生产可落地**：标准库实现便于教学与审计，Docker/K8s/KubeEdge 清单可直接上生产；
4. **排障经验工程化**：把「现象→取证→假设→处置」的方法论固化成知识库与确定性规则，LLM 不可用也能自愈。

---

## 11. 与开源之夏（OSPP）/ KubeEdge 的关系

本项目可作为 **KubeEdge / KubeEdge Sedna（边云协同 AI）** 方向的 OSPP 候选提案，
也可作为个人作品集证明「云原生 + 边缘 + LLM」的复合工程能力。它对标的能力包括：
KubeEdge 云边通信机制理解、边缘 AI 推理部署、以及运维智能化（AIOps）方向。

后续路线：接入真实 vLLM 与多模态模型、增加 EdgeMesh 服务发现、把 RCA 提示词做成
可微调的小模型、补充 Grafana/Prometheus 可观测看板。

# 与 KubeEdge 集成说明

EdgeMind 的云端/边端通信与控制模型，**刻意对齐 KubeEdge 的真实架构**，
因此可直接运行在 KubeEdge 纳管的集群上。

## 概念映射（EdgeMind ↔ KubeEdge）

| EdgeMind 组件 | KubeEdge 对应 | 职责 |
|---|---|---|
| `CloudHub` | cloudcore 的 **cloudhub** | 云端 WebSocket 网关，边端长连接入口 |
| `Controller` | **edgeController** + **deviceController** | 边缘节点/设备状态与下发控制 |
| `EdgeHub` | edgecore 的 **edgehub** | 边端长连接客户端，自动重连 |
| `EdgeAgent` | edgecore 上的**业务负载** | 本地推理 + 执行云端指令 |
| 心跳/指标/指令 Envelope | KubeEdge 的 **resource / message** 路由 | 云边消息契约 |
| `vllm-gateway` | ——（上层推理服务） | 云端大模型推理 |
| `diagnoser` | ——（智能运维扩展） | LLM 故障自愈，KubeEdge 之上叠加的 Agent 能力 |

## 部署路径

1. **云端**：用 KubeEdge 官方 Helm 安装 cloudcore（见 `cloudcore-values.yaml`），
   或用本项目 `deploy/k8s/cloud-deployment.yaml` 直接部署 EdgeMind 控制面。
2. **边端**：按 KubeEdge 文档把边缘节点加入集群（生成边缘节点专属 token，
   安装 edgecore），节点获得 `node-role.kubernetes.io/edge` 标签。
3. **调度 EdgeAgent**：本项目 `deploy/k8s/edge-daemonset.yaml` 用 nodeSelector
   选中边缘节点，每个节点跑一个 EdgeAgent；它通过 `EDGE_CLOUD_WS_URL`
   连到云端 CloudHub。
4. **推理服务**：云端独立部署 vLLM（`cloud-deployment.yaml` 中已含），
   EdgeAgent 在本地不可用时自动把请求路由到云端 vLLM。

## 弱网/离线能力（KubeEdge 核心价值）

- EdgeHub 内置断线重连与消息缓存，弱网切换不丢控制指令；
- 边端本地推理（LocalInference）在云边断连时仍可离线服务；
- 云端 `heartbeat_timeout` 检测离线，诊断 Agent 触发迁云/重启/告警。

> 注：`deploy/k8s/kubeedge-crds.yaml` 中的 EdgeNode / Device 是 KubeEdge 自带 CRD，
> EdgeMind 读取它们来获得边缘拓扑与设备孪生，无需自行定义。

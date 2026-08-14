# 五维能力对照表（直接回应项目需求）

本文件把「需求要求的 5 项能力」逐条映射到项目的**具体文件 / 命令 / 设计点**，
方便在简历、OSPP 申请书或面试中作为证据。

---

## ① 掌握 Python，具备良好的工程开发能力

**证据**
- 全量代码 Python 3.12，**纯标准库实现云边通信与 HTTP 控制面**（零第三方依赖即可运行），
  体现对语言与网络原语的掌握。
- 工程化实践贯穿始终：
  - 类型注解（`dataclass` / `Optional` / `typing`）
  - 统一日志（`common/logging_setup.py`，按组件配色）
  - 集中配置（`common/config.py`，环境变量覆盖，不硬编码）
  - 模块化分层：`common / cloud / edge / agent` 职责清晰
  - 单元测试（`tests/`，协议往返、WebSocket 收发、诊断闭环）
- 关键技术点：自研 WebSocket 帧封装（掩码/扩展长度/ping-pong/重连）、
  多线程并发（云端多连接、边端心跳、诊断循环互不阻塞）。

**可演示命令**
```bash
python tests/test_protocol.py && python tests/test_ws.py && python tests/test_diagnoser.py
python demo/run_demo.py
```

---

## ② 熟悉 Linux、Docker 和 Kubernetes

**证据**
- **Docker**：`deploy/docker/Dockerfile.cloud` / `Dockerfile.edge`（基于 `python:3.12-slim`，
  非 root 友好的精简镜像）、`docker-compose.yml` 一键编排云+2边+可选 vLLM。
- **Kubernetes**：`deploy/k8s/` 包含
  - `cloud-deployment.yaml`：云端 Deployment（2 副本）+ Service + 就绪/存活探针 + 独立 vLLM Deployment；
  - `edge-daemonset.yaml`：边端 DaemonSet，用 `nodeSelector` 选中 KubeEdge 边缘节点，
    `hostNetwork` 访问本地设备，`downward API` 注入节点名作为边端 ID；
  - `configmap.yaml`：配置与密钥解耦。
- **Linux**：进程/线程模型、socket 编程、信号处理（Ctrl+C 优雅退出）、主机网络与设备访问。

**可演示命令**
```bash
docker compose -f deploy/docker/docker-compose.yml up --build
kubectl apply -f deploy/k8s/
```

---

## ③ 熟悉 KubeEdge 架构及云边通信机制

**证据**
- 完整复现 KubeEdge 的云边通信主干：
  - `CloudHub` ↔ `EdgeHub` 的 **WebSocket 长连接**（对齐 cloudcore.cloudhub / edgecore.edgehub）；
  - `Controller` 对齐 **edgeController / deviceController**（节点与设备控制）；
  - `Envelope` 消息契约对齐 KubeEdge 的 `operation + resource` 路由。
- 手写 `common/ws.py`：RFC6455 握手、掩码、扩展长度、ping/pong、**断线指数退避重连**——
  这些都是 KubeEdge 云边隧道（websocket / quic）的核心机制。
- `deploy/kubeedge/`：cloudcore Helm values 对照 + EdgeNode/Device CRD 使用示例，
  明确本项目与 KubeEdge 的**概念映射与部署路径**。

**关键文件**：`src/common/ws.py`、`src/cloud/cloudhub.py`、`src/edge/edgehub.py`、`deploy/kubeedge/README.md`

---

## ④ 了解 vLLM 等大模型推理服务

**证据**
- `src/cloud/vllm_gateway.py`：**以 OpenAI 兼容接口对接 vLLM**
  （`POST {base_url}/v1/chat/completions`），这是 vLLM 对外服务的标准方式。
- **云边协同推理**：云端 vLLM 跑大模型，边端跑本地轻量模型（`LocalInference`），
  按健康/时延/离线状态在二者间路由，体现对「推理服务分层部署」的理解。
- **离线降级**：`VLLM_BASE_URL` 为空时走确定性 mock，保证无 GPU 也能演示；
  配置真实地址即可接入生产 vLLM（Docker Compose 中已给出 vLLM 服务示例）。
- K8s 中 vLLM 作为独立 Deployment + GPU 资源声明（`nvidia.com/gpu: 1`），可独立扩缩容。

**关键文件**：`src/cloud/vllm_gateway.py`、`src/edge/local_inference.py`、`deploy/k8s/cloud-deployment.yaml`

---

## ⑤ 了解 LLM Agent，有故障排查经验者优先

**证据**
- **LLM Agent（诊断自愈）**：`src/agent/diagnoser.py` 周期巡检，调用 vLLM 做
  **根因分析（RCA）**，要求返回结构化 JSON 决策（根因 + 动作白名单）；
  解析失败/不可用时**回退确定性规则**——这是真实的排障兜底经验。
- **修复剧本（playbook）**：`src/agent/playbook.py` 把 Agent 的建议落地为真实动作，
  动作**白名单化**保证安全（模型只给建议，系统负责执行）。
- **故障排查经验工程化**：`src/agent/troubleshoot.py` 固化了
  「现象 → 取证 → 假设验证 → 处置」的四步排障方法论，并提供 `--live` 实时巡检。
- **覆盖的真实故障**：心跳丢失、GPU OOM、模型加载失败、时延突增，均有一致的根因与处置路径。
- **演示即证据**：`demo/run_demo.py` 自动注入上述故障并展示 Agent 自动自愈全过程。

**关键文件**：`src/agent/diagnoser.py`、`src/agent/playbook.py`、`src/agent/troubleshoot.py`、`src/agent/prompts.py`

**可演示命令**
```bash
python demo/run_demo.py                 # 看自动自愈
python -m agent.troubleshoot --list     # 列出支持的故障类型
python -m agent.troubleshoot --kind gpu_oom   # 查看某类故障的排障手册
python -m agent.troubleshoot --live     # 实时巡检运行中的系统
```

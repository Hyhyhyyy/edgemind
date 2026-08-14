"""云端控制面：边缘节点控制器（模拟 KubeEdge EdgeController + 部分 CloudCore）。

职责：
- 维护边缘节点注册表与实时状态（相位、心跳、指标）；
- 检测心跳超时并生成 heartbeat_lost 故障事件；
- 记录事件（Incident）供诊断 Agent 消费；
- 提供一组「修复动作」（playbook 的落点），由诊断 Agent 调度，
  动作要么改本地状态（reschedule/cordon），要么通过 CloudHub 向边端
  下发 COMMAND（restart/fallback），要么模拟 K8s 运维操作（scale）。

这是整个系统的「大脑状态库」，CloudHub / API / Diagnoser 都围绕它协作。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from common.config import CONFIG
from common.logging_setup import get_logger
from common.protocol import (
    Envelope,
    MsgType,
    NodeMetrics,
    NodePhase,
    Incident,
)

logger = get_logger("cloud.controller", "cloud")


@dataclass
class EdgeNode:
    node_id: str
    region: str = "default"
    gpu: str = "none"
    phase: NodePhase = NodePhase.PENDING
    last_heartbeat: int = 0
    last_metrics: NodeMetrics = field(default_factory=NodeMetrics)
    pending_workloads: int = 0
    ops_log: list[str] = field(default_factory=list)

    def heartbeat_age(self) -> float:
        return (time.time() * 1000 - self.last_heartbeat) / 1000.0


@dataclass
class ActionRecord:
    incident_id: str
    action: str
    detail: str
    at: int = field(default_factory=lambda: int(time.time() * 1000))


class Controller:
    def __init__(self, cloudhub=None, vllm_gateway=None):
        self.cloudhub = cloudhub
        self.vllm = vllm_gateway
        self._lock = threading.RLock()
        self.nodes: dict[str, EdgeNode] = {}
        self.incidents: list[Incident] = []
        self.actions: list[ActionRecord] = []

    # ---- 云边消息接入（由 CloudHub 调用）----
    def handle(self, env: Envelope) -> None:
        t = env.type
        if t == MsgType.REGISTER.value:
            self._register(env)
        elif t == MsgType.HEARTBEAT.value:
            self._heartbeat(env)
        elif t == MsgType.METRICS.value:
            self._metrics(env)
        elif t == MsgType.EVENT.value:
            self._event(env)
        elif t == MsgType.ACK.value:
            self._ack(env)

    def _register(self, env: Envelope) -> None:
        p = env.payload
        with self._lock:
            node = self.nodes.get(env.node_id) or EdgeNode(node_id=env.node_id)
            node.region = p.get("region", node.region)
            node.gpu = p.get("gpu", node.gpu)
            node.phase = NodePhase.RUNNING
            node.last_heartbeat = int(time.time() * 1000)
            self.nodes[env.node_id] = node
        logger.info("边缘节点上线: %s (region=%s, gpu=%s)", env.node_id, node.region, node.gpu)
        # 回 ACK
        if self.cloudhub:
            self.cloudhub.send(env.node_id, Envelope.build(MsgType.ACK, env.node_id,
                                                           {"for": "register"}).to_json())

    def _heartbeat(self, env: Envelope) -> None:
        with self._lock:
            node = self.nodes.get(env.node_id)
            if node:
                node.last_heartbeat = int(time.time() * 1000)
                if node.phase in (NodePhase.NOT_READY, NodePhase.OFFLINE):
                    node.phase = NodePhase.RUNNING
                    logger.info("节点 %s 恢复在线", env.node_id)

    def _metrics(self, env: Envelope) -> None:
        with self._lock:
            node = self.nodes.get(env.node_id)
            if node:
                node.last_metrics = NodeMetrics(**{k: v for k, v in env.payload.items()
                                                   if k in NodeMetrics.__dataclass_fields__})
                node.last_heartbeat = int(time.time() * 1000)

    def _event(self, env: Envelope) -> None:
        p = env.payload
        severity = p.get("severity", "warning")
        if severity == "info":
            # 信息级事件（如运行时重启完成）只记录，不生成故障工单
            logger.info("边端信息事件 %s: %s", p.get("kind"), p.get("message", ""))
            return
        inc = Incident(node_id=env.node_id, kind=p.get("kind", "event"),
                       severity=severity,
                       message=p.get("message", ""), context=p)
        with self._lock:
            self.incidents.append(inc)
        logger.warning("收到边端事件 %s: %s", inc.kind, inc.message)

    def _ack(self, env: Envelope) -> None:
        logger.debug("收到 ACK: %s", env.payload.get("for"))

    # ---- 定时器：心跳超时检测 ----
    def tick(self) -> list[Incident]:
        """由诊断循环周期性调用：标记超时节点并生成 heartbeat_lost 事件。"""
        now = int(time.time() * 1000)
        new_incidents: list[Incident] = []
        with self._lock:
            for node in self.nodes.values():
                if node.phase in (NodePhase.PENDING, NodePhase.CORDONED):
                    continue
                age = node.heartbeat_age()
                if age > CONFIG.heartbeat_timeout and node.phase != NodePhase.OFFLINE:
                    node.phase = NodePhase.OFFLINE
                    inc = Incident(node_id=node.node_id, kind="heartbeat_lost",
                                   severity="critical",
                                   message=f"心跳超时 {age:.1f}s 未收到，判定离线",
                                   context={"age_s": round(age, 1)})
                    self.incidents.append(inc)
                    new_incidents.append(inc)
                    logger.error("节点 %s 心跳丢失，已判定 OFFLINE", node.node_id)
        return new_incidents

    # ---- 查询 ----
    def get_nodes(self) -> list[EdgeNode]:
        with self._lock:
            return list(self.nodes.values())

    def get_node(self, node_id: str) -> Optional[EdgeNode]:
        with self._lock:
            return self.nodes.get(node_id)

    def open_incidents(self) -> list[Incident]:
        with self._lock:
            return [i for i in self.incidents if i.context.get("status", "open") == "open"]

    # ---- 修复动作（playbook 落点）----
    def _record(self, incident_id: str, action: str, detail: str) -> None:
        with self._lock:
            self.actions.append(ActionRecord(incident_id, action, detail))
        logger.info("[playbook] %s -> %s | %s", incident_id, action, detail)

    def _send_command(self, node_id: str, command: str, params: dict) -> None:
        if self.cloudhub:
            env = Envelope.build(MsgType.COMMAND, node_id,
                                 {"command": command, "params": params})
            self.cloudhub.send(node_id, env.to_json())
        logger.info("向 %s 下发指令: %s %s", node_id, command, params)

    def resolve_incident(self, incident: Incident, resolution: str) -> None:
        with self._lock:
            incident.context["status"] = "resolved"
            incident.context["resolution"] = resolution

    # 具体动作
    def mark_not_ready(self, node_id: str, incident_id: str) -> None:
        with self._lock:
            node = self.nodes.get(node_id)
            if node and node.phase != NodePhase.OFFLINE:
                node.phase = NodePhase.NOT_READY
        self._record(incident_id, "mark_not_ready", f"{node_id} 标记为 NotReady，停止接新流量")

    def reschedule_to_cloud(self, node_id: str, incident_id: str) -> None:
        with self._lock:
            node = self.nodes.get(node_id)
            moved = node.pending_workloads if node else 0
            if node:
                node.pending_workloads = 0
        self._record(incident_id, "reschedule_to_cloud",
                     f"将 {node_id} 的 {moved} 个待推理任务迁移到云端 vLLM 处理")

    def cordon(self, node_id: str, incident_id: str) -> None:
        with self._lock:
            node = self.nodes.get(node_id)
            if node:
                node.phase = NodePhase.CORDONED
        self._record(incident_id, "cordon", f"封锁 {node_id}，禁止新调度")

    def fallback_edge_to_small_model(self, node_id: str, incident_id: str) -> None:
        self._send_command(node_id, "load_model", {"model": "mock-mini-0.5b"})
        self._record(incident_id, "fallback_edge_to_small_model", "边端降级加载小模型以释放显存")

    def route_inference_to_cloud(self, node_id: str, incident_id: str) -> None:
        with self._lock:
            node = self.nodes.get(node_id)
            if node:
                node.pending_workloads = 0
        self._record(incident_id, "route_inference_to_cloud", f"{node_id} 的推理请求改路由到云端")

    def restart_edge_runtime(self, node_id: str, incident_id: str) -> None:
        self._send_command(node_id, "restart_runtime", {})
        self._record(incident_id, "restart_edge_runtime", f"远程重启 {node_id} 的本地推理运行时")

    def restart_edge_node(self, node_id: str, incident_id: str) -> None:
        self._send_command(node_id, "reboot", {})
        self._record(incident_id, "reboot", f"触发 {node_id} 重启（kubeedge edgecore 重拉）")

    def scale_cloud_replicas(self, incident_id: str, delta: int = 1) -> None:
        # 模拟 kubectl scale deployment vllm --replicas=+delta
        self._record(incident_id, "scale_cloud_replicas",
                     f"kubectl scale vllm-gateway --replicas=+{delta}（缓解云端排队）")

    def route_to_local_edge(self, node_id: str, incident_id: str) -> None:
        self._record(incident_id, "route_to_local_edge", f"将部分请求下沉到边端 {node_id} 本地推理")

    def throttle_low_priority(self, node_id: str, incident_id: str) -> None:
        self._record(incident_id, "throttle_low_priority", "对非实时低优请求限流，保 SLA")

    def alert_oncall(self, node_id: str, incident_id: str) -> None:
        self._record(incident_id, "alert_oncall",
                     f"已推送告警给 oncall：节点 {node_id} 需要人工关注")

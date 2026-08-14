"""EdgeMind 云边协同消息协议定义。

设计目标：用一套最小但严谨的 JSON 消息契约，模拟 KubeEdge 中
EdgeHub <-> CloudHub 之间的 resource / message 通信模型。

KubeEdge 真实组件映射（详见 docs/architecture.md）：
- EdgeHub  <->  本协议的 EdgeClient（边端长连接客户端）
- CloudHub <->  本协议 CloudHub（云端 WebSocket 网关）
- 消息路由：KubeEdge 用 operation + resource 字段路由到
  EdgeController / DeviceController；这里用同样的思路做 node/metrics/command。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class MsgType(str, Enum):
    """消息类型，对应 KubeEdge 中的 operation。"""

    HEARTBEAT = "heartbeat"          # 边端->云：心跳保活
    REGISTER = "register"            # 边端->云：节点上线注册
    METRICS = "metrics"              # 边端->云：上报指标
    EVENT = "event"                  # 边端->云：本地事件（如模型加载失败）
    COMMAND = "command"              # 云->边：下发的控制指令
    ROUTE = "route"                  # 云<->边：推理请求路由（云边协同推理）
    ACK = "ack"                      # 通用确认
    STATUS = "status"               # 云->边 / 云内部：节点状态变更


class NodePhase(str, Enum):
    """边缘节点生命周期相位，对齐 K8s Node 的 Ready/NotReady。"""

    PENDING = "Pending"
    RUNNING = "Running"
    NOT_READY = "NotReady"
    OFFLINE = "Offline"
    CORDONED = "Cordoned"            # 已封锁，不再调度新负载


@dataclass
class Envelope:
    """所有云边消息的统一信封。

    seq 用于去重与 ACK 配对；ts 为发送端毫秒时间戳。
    """

    type: str
    node_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    seq: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_json(self) -> str:
        return __import__("json").dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Envelope":
        obj = __import__("json").loads(raw)
        return cls(
            type=obj["type"],
            node_id=obj["node_id"],
            payload=obj.get("payload", {}),
            seq=obj.get("seq", uuid.uuid4().hex[:12]),
            ts=obj.get("ts", int(time.time() * 1000)),
        )

    @classmethod
    def build(cls, type: MsgType, node_id: str, payload: dict[str, Any]) -> "Envelope":
        return cls(type=type.value, node_id=node_id, payload=payload)


@dataclass
class NodeMetrics:
    """边端周期性上报的资源/推理指标。"""

    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    gpu_util_percent: Optional[float] = None
    gpu_mem_percent: Optional[float] = None
    net_rtt_ms: float = 0.0            # 到云端的网络往返时延
    inf_queue: int = 0                 # 本地推理队列长度
    inf_p95_ms: float = 0.0            # 本地推理 P95 时延
    model_loaded: Optional[str] = None  # 当前加载的本地模型名
    healthy: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Incident:
    """一次故障/异常事件的结构化记录，供诊断 Agent 消费。"""

    incident_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    node_id: str = ""
    kind: str = ""                     # 如 heartbeat_lost / gpu_oom / latency_spike
    severity: str = "info"             # info | warning | critical
    message: str = ""
    observed_at: int = field(default_factory=lambda: int(time.time() * 1000))
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

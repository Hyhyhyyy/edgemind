"""故障演示场景定义。

每个场景：(节点, 故障类型, 触发延迟秒)。run_demo 会按时间线注入，
由诊断 Agent 自动检测并自愈，用于直观展示「云边协同 + LLM 自愈」。
"""
from __future__ import annotations

SCENARIOS = [
    # 节点 edge-2 网络分区：心跳丢失 -> 标记 NotReady + 任务迁云 + 告警
    ("edge-2", "heartbeat_lost", 6),
    # 节点 edge-1 GPU OOM：本地模型被杀 -> 降级小模型 + 路由到云 + 重启运行时
    ("edge-1", "gpu_oom", 14),
    # 节点 edge-1 时延突增：云端扩容 + 下沉边端 + 限流
    ("edge-1", "latency_spike", 24),
]

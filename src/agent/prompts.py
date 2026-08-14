"""诊断 Agent 的提示词工程（RCA / 决策）。

把故障上下文交给 vLLM（大模型推理服务）做根因分析，
约束其只输出我们可执行的 JSON 决策——这是把「LLM Agent」与
「真实运维动作」安全桥接的关键：模型只给建议，playbook 负责落地，
且动作词汇白名单化，避免模型越权。
"""

# 可供模型选择的「修复动作」白名单（必须与 agent/playbook.py 一致）
ACTION_VOCAB = [
    "mark_not_ready",
    "reschedule_to_cloud",
    "cordon",
    "fallback_edge_to_small_model",
    "route_inference_to_cloud",
    "restart_edge_runtime",
    "restart_edge_node",
    "scale_cloud_replicas",
    "route_to_local_edge",
    "throttle_low_priority",
    "alert_oncall",
]

RCA_SYSTEM_PROMPT = """你是一名资深 SRE，负责一个云边协同的大模型推理平台 EdgeMind。
云端用 vLLM 跑大模型，边缘节点（KubeEdge 管理）跑本地轻量模型，云边通过
WebSocket 长连接协同。请你基于给定的故障上下文做根因分析(RCA)并给出修复决策。

你【只能】输出如下严格 JSON，不要输出任何多余文字：
{
  "root_cause": "一句话根因",
  "severity": "critical | warning | info",
  "confidence": 0.0~1.0,
  "actions": ["从白名单中挑选的0~多个动作"],
  "rationale": "简短的推理说明"
}

动作白名单（含义）：
- mark_not_ready: 标记节点 NotReady，停止接新流量
- reschedule_to_cloud: 把该节点待处理推理任务迁移到云端
- cordon: 封锁节点，禁止新调度
- fallback_edge_to_small_model: 边端降级加载更小模型以释放显存
- route_inference_to_cloud: 将该节点推理请求改路由到云端
- restart_edge_runtime: 远程重启边端本地推理运行时
- restart_edge_node: 触发边端节点重启
- scale_cloud_replicas: 扩容云端 vLLM 副本以缓解排队
- route_to_local_edge: 将已知请求下沉到边端本地推理
- throttle_low_priority: 对非实时低优请求限流保 SLA
- alert_oncall: 推送告警给值班人员人工介入

请基于场景选择最贴切、最少必要动作（通常 1~3 个）。"""

RCA_USER_TEMPLATE = """## 故障上下文
incident_id={incident_id}
node_id={node_id}
kind={kind}
severity_input={severity}
message={message}
metrics={metrics}
recent_actions={recent_actions}

请输出 JSON 决策。"""


def build_rca_messages(incident, metrics: dict, recent_actions: list) -> list:
    user = RCA_USER_TEMPLATE.format(
        incident_id=incident.incident_id,
        node_id=incident.node_id,
        kind=incident.kind,
        severity=incident.severity,
        message=incident.message,
        metrics=metrics,
        recent_actions=recent_actions,
    )
    return [
        {"role": "system", "content": RCA_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]

"""修复剧本执行器（playbook）。

把诊断 Agent 给出的「动作词」翻译成对 Controller 的真实调用。
这是安全边界：Agent（LLM）只产出白名单内的动作名，playbook 负责
真正落地，且每个动作都有明确语义与日志，便于审计与回滚。
"""
from __future__ import annotations

from typing import Iterable

from common.logging_setup import get_logger
from cloud.controller import Controller
from common.protocol import Incident

logger = get_logger("agent.playbook", "agent")

# 动作白名单（与 prompts.ACTION_VOCAB 同步）
KNOWN_ACTIONS = {
    "mark_not_ready", "reschedule_to_cloud", "cordon",
    "fallback_edge_to_small_model", "route_inference_to_cloud",
    "restart_edge_runtime", "restart_edge_node", "scale_cloud_replicas",
    "route_to_local_edge", "throttle_low_priority", "alert_oncall",
}


def execute(controller: Controller, incident: Incident, actions: Iterable[str],
            dry_run: bool = False) -> list[str]:
    """执行一组动作，返回实际执行的动作名列表。"""
    done = []
    for action in actions:
        if action not in KNOWN_ACTIONS:
            logger.warning("跳过未知动作: %s", action)
            continue
        if dry_run:
            logger.info("[dry_run] 将执行 %s (incident=%s)", action, incident.incident_id)
            done.append(action)
            continue
        _dispatch(controller, incident, action)
        done.append(action)
    return done


def _dispatch(controller: Controller, incident: Incident, action: str) -> None:
    nid = incident.node_id
    iid = incident.incident_id
    fn = getattr(controller, action, None)
    if fn is None:
        logger.warning("Controller 无对应方法: %s", action)
        return
    # 不同动作的参数签名不同
    if action == "scale_cloud_replicas":
        fn(iid)
    elif action in ("alert_oncall", "route_to_local_edge"):
        fn(nid, iid)
    else:
        fn(nid, iid)

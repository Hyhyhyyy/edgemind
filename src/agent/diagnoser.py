"""诊断自愈 Agent（LLM Agent + 故障排查经验的核心）。

工作循环（每 diag_interval 秒）：
1. controller.tick() —— 检测心跳超时，生成 heartbeat_lost 事件；
2. _detect_anomalies() —— 从边端上报指标中识别时延/显存异常，去重生成事件；
3. 对每条「未处理」的故障事件做 RCA：
   - 调 vLLM（云端大模型推理网关）做根因分析，要求输出结构化 JSON 决策；
   - 解析失败或 vLLM 不可用时，回退到确定性规则（体现「故障排查经验」）；
4. 把决策里的动作交给 playbook 执行（真实改状态/下发指令）；
5. 闭环：标记事件已解决，并记录根因摘要。

安全设计：LLM 只产出白名单动作；dry_run 模式下只打印不落地。
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

from common.config import CONFIG
from common.logging_setup import get_logger
from common.protocol import Incident, NodeMetrics
from cloud.controller import Controller
from cloud.vllm_gateway import VLLMGateway, ChatMessage
from agent import prompts
from agent.playbook import execute

logger = get_logger("agent.diagnoser", "agent")

# 确定性规则兜底（无 LLM / 解析失败时使用）
_FALLBACK = {
    "heartbeat_lost": ("critical", ["mark_not_ready", "reschedule_to_cloud", "alert_oncall"]),
    "gpu_oom": ("warning", ["fallback_edge_to_small_model", "route_inference_to_cloud", "restart_edge_runtime"]),
    "model_load_fail": ("warning", ["route_inference_to_cloud", "restart_edge_runtime", "alert_oncall"]),
    "latency_spike": ("warning", ["scale_cloud_replicas", "route_to_local_edge", "throttle_low_priority"]),
    "node_unhealthy": ("warning", ["alert_oncall"]),
}


class Diagnoser:
    def __init__(self, controller: Controller, vllm: VLLMGateway):
        self.controller = controller
        self.vllm = vllm
        self.dry_run = CONFIG.dry_run
        self._processed: set[str] = set()
        self._active_anomalies: set[tuple] = set()  # (node_id, kind) 正在持续的异常，去重避免反复建单
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("诊断自愈 Agent 启动（dry_run=%s）", self.dry_run)

    def stop(self) -> None:
        self._stop.set()

    # ---- 主循环 ----
    def _loop(self) -> None:
        while not self._stop.is_set():
            self.diagnose_once()
            time.sleep(CONFIG.diag_interval)

    def diagnose_once(self) -> list[dict]:
        """执行一轮诊断，返回本轮处理摘要（供演示/测试断言）。"""
        self.controller.tick()
        self._detect_anomalies()
        summaries = []
        for inc in self.controller.open_incidents():
            if inc.incident_id in self._processed:
                continue
            self._processed.add(inc.incident_id)
            summary = self._handle_incident(inc)
            summaries.append(summary)
        return summaries

    # ---- 指标异常检测 ----
    def _detect_anomalies(self) -> None:
        """从边端指标识别异常；持续中的异常只建一次工单，恢复后清除标记。"""
        open_kinds = {(i.node_id, i.kind) for i in self.controller.open_incidents()}
        for node in self.controller.get_nodes():
            m = node.last_metrics
            # 时延/网络异常
            lat_key = (node.node_id, "latency_spike")
            if m.inf_p95_ms > 1000 or m.net_rtt_ms > 300:
                if lat_key not in self._active_anomalies:
                    self._active_anomalies.add(lat_key)
                    self._mk_incident(node.node_id, "latency_spike", "warning",
                                      f"推理 P95={m.inf_p95_ms:.0f}ms / RTT={m.net_rtt_ms:.0f}ms 超 SLA",
                                      {"inf_p95_ms": m.inf_p95_ms, "net_rtt_ms": m.net_rtt_ms})
            else:
                self._active_anomalies.discard(lat_key)
            # 显存打满（边端可能已通过 EVENT 建单，避免重复）
            gpu_key = (node.node_id, "gpu_oom")
            if m.gpu_mem_percent and m.gpu_mem_percent > 92:
                if gpu_key not in self._active_anomalies:
                    self._active_anomalies.add(gpu_key)
                    if gpu_key not in open_kinds:
                        self._mk_incident(node.node_id, "gpu_oom", "warning",
                                          f"GPU 显存 {m.gpu_mem_percent:.0f}% 濒临 OOM",
                                          {"gpu_mem_percent": m.gpu_mem_percent})
            else:
                self._active_anomalies.discard(gpu_key)

    def _mk_incident(self, node_id, kind, severity, message, ctx) -> None:
        inc = Incident(node_id=node_id, kind=kind, severity=severity, message=message, context=ctx)
        with self.controller._lock:
            self.controller.incidents.append(inc)
        logger.warning("检测到异常 %s @ %s: %s", kind, node_id, message)

    # ---- 单条事件处理 ----
    def _handle_incident(self, inc: Incident) -> dict:
        node = self.controller.get_node(inc.node_id)
        metrics = node.last_metrics.to_dict() if node else {}
        decision = self._rca(inc, metrics)
        logger.info("RCA[%s] root_cause=%s | actions=%s",
                    inc.incident_id, decision["root_cause"], decision["actions"])
        executed = execute(self.controller, inc, decision["actions"], dry_run=self.dry_run)
        resolution = f"根因: {decision['root_cause']}；动作: {','.join(executed) or '无'}"
        self.controller.resolve_incident(inc, resolution)
        return {
            "incident_id": inc.incident_id,
            "node_id": inc.node_id,
            "kind": inc.kind,
            "root_cause": decision["root_cause"],
            "severity": decision["severity"],
            "actions": executed,
            "source": decision["source"],
        }

    def _rca(self, inc: Incident, metrics: dict) -> dict:
        """调用 vLLM 做根因分析；失败则回退规则。"""
        msgs = prompts.build_rca_messages(inc, metrics, recent_actions=[])
        chat_msgs = [ChatMessage(role=m["role"], content=m["content"]) for m in msgs]
        try:
            raw = self.vllm.chat(chat_msgs, temperature=0.1)
            parsed = self._extract_json(raw)
            if parsed and "root_cause" in parsed:
                actions = [a for a in parsed.get("actions", []) if a in prompts.ACTION_VOCAB]
                return {
                    "root_cause": parsed.get("root_cause", inc.message),
                    "severity": parsed.get("severity", inc.severity),
                    "actions": actions or _FALLBACK.get(inc.kind, (inc.severity, ["alert_oncall"]))[1],
                    "source": "llm",
                }
        except Exception as e:
            logger.warning("RCA 调用/解析失败，回退规则: %s", e)
        sev, acts = _FALLBACK.get(inc.kind, (inc.severity, ["alert_oncall"]))
        return {"root_cause": f"[规则兜底] {inc.message}", "severity": sev, "actions": acts, "source": "rule"}

    @staticmethod
    def _extract_json(raw: str) -> Optional[dict]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # 去掉 ```json 代码围栏，截取首个 { 到末个 }
        cleaned = re.sub(r"```(?:json)?", "", raw).strip()
        s, e = cleaned.find("{"), cleaned.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(cleaned[s:e + 1])
            except json.JSONDecodeError:
                return None
        return None

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from common.config import CONFIG
from common.protocol import Incident
from cloud.controller import Controller
from cloud.vllm_gateway import VLLMGateway
from agent.diagnoser import Diagnoser

CONFIG.dry_run = False


def test_diagnoser_resolves_gpu_oom():
    vllm = VLLMGateway(base_url="", model="mock-7b")  # mock 模式，离线可跑
    controller = Controller(vllm_gateway=vllm)
    controller.cloudhub = None
    diagnoser = Diagnoser(controller, vllm)

    inc = Incident(node_id="edge-1", kind="gpu_oom", severity="warning",
                   message="GPU 显存耗尽")
    controller.incidents.append(inc)

    summaries = diagnoser.diagnose_once()
    assert any(s["incident_id"] == inc.incident_id for s in summaries), "事件未被处理"
    # 事件应被闭环
    assert inc.context.get("status") == "resolved", inc.context
    # 应执行了与 gpu_oom 相关的修复动作（mock 规则兜底或 LLM）
    actions = [a.action for a in controller.actions]
    assert "route_inference_to_cloud" in actions or "fallback_edge_to_small_model" in actions, actions
    print("PASS test_diagnoser_resolves_gpu_oom  actions=%s" % actions)


if __name__ == "__main__":
    test_diagnoser_resolves_gpu_oom()

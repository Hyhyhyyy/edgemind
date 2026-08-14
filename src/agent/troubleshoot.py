"""故障排查 CLI（展示「故障排查经验」的方法论）。

不依赖 LLM，是一份结构化的排障知识库 + 可选「实时巡检」模式。
用法：
  python -m agent.troubleshoot --kind gpu_oom          # 打印某类故障的排障手册
  python -m agent.troubleshoot --list                  # 列出支持的故障类型
  python -m agent.troubleshoot --live                  # 连接本地运行的系统做实时巡检

排障四步法：现象 -> 取证 -> 假设验证 -> 处置（对应 RCA 的工程化落地）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from common.logging_setup import get_logger

logger = get_logger("agent.troubleshoot", "agent")

# 排障知识库：每类故障给出系统化的排查路径
KB = {
    "heartbeat_lost": {
        "symptom": "边缘节点在控制台变为 NotReady/Offline，心跳 age 持续增长。",
        "evidence": [
            "查 cloudhub 连接表：该 node_id 是否还有在线 WebSocket peer",
            "边端看 edgecore / edgehub 日志：是否 ESTABLISHED 断开、重连是否失败",
            "网络：边端到云端 9000 端口连通性、TLS/代理、NAT 超时",
            "资源：边端是否断电/重启、进程是否 OOM 被杀",
        ],
        "hypotheses": [
            "弱网/断电 -> 长连接中断且无重连 (H1)",
            "云端 cloudhub 重启丢弃了路由表 -> 边端重连后可恢复 (H2)",
            "边端进程崩溃 -> 需重启 edgecore (H3)",
        ],
        "root_cause": "云边 WebSocket 长连接中断，超过心跳超时阈值未恢复。",
        "remediation": [
            "mark_not_ready：先隔离，避免把流量打到已不可达节点",
            "reschedule_to_cloud：把该节点待推理任务迁移到云端 vLLM",
            "若 H3：restart_edge_node 触发 edgecore 重拉并自动重连",
            "alert_oncall：跨机房/长时间离线必须人工确认",
        ],
    },
    "gpu_oom": {
        "symptom": "边端本地推理进程退出，model_loaded 变空，GPU 显存占用高。",
        "evidence": [
            "nvidia-smi：显存占用、是否被其他进程挤占",
            "边端日志：是否有 CUDA out of memory / 推理进程退出码",
            "当前加载模型规格与显存容量是否匹配",
        ],
        "hypotheses": [
            "模型规格超过显存 -> OOM (H1)",
            "并发请求叠加导致瞬时显存峰值 -> OOM (H2)",
        ],
        "root_cause": "边端 GPU 显存不足，本地大模型推理进程 OOM 被杀。",
        "remediation": [
            "fallback_edge_to_small_model：降级加载更小模型释放显存",
            "route_inference_to_cloud：把推理请求临时改路由到云端",
            "restart_edge_runtime：清理残留显存后重启本地运行时",
        ],
    },
    "model_load_fail": {
        "symptom": "边端启动后本地模型一直为空，推理不可用。",
        "evidence": [
            "模型权重来源（镜像仓库/对象存储）是否可达",
            "边端磁盘剩余空间、挂载点",
            "模型文件完整性（hash）",
        ],
        "hypotheses": [
            "仓库/网络不可达 -> 拉取失败 (H1)",
            "磁盘满 -> 写入失败 (H2)",
        ],
        "root_cause": "边端拉取/加载模型权重失败，本地推理不可用。",
        "remediation": [
            "route_inference_to_cloud：本地不可用期间请求走云端",
            "restart_edge_runtime：恢复后重试加载",
            "alert_oncall：持续失败需人工排查存储/网络",
        ],
    },
    "latency_spike": {
        "symptom": "端到端推理 P95 陡增，部分请求超 SLA。",
        "evidence": [
            "区分是云端排队还是云边网络：看 cloud vLLM 队列长度 vs 边端到云 RTT",
            "边端 inf_p95_ms 与 net_rtt_ms 分别贡献多少",
        ],
        "hypotheses": [
            "云端 vLLM 副本不足、队列堆积 -> 云端瓶颈 (H1)",
            "跨机房 RTT 突增 / 弱网 -> 网络瓶颈 (H2)",
        ],
        "root_cause": "云端 vLLM 排队过高或云边 RTT 突增，导致端到端 P95 超 SLA。",
        "remediation": [
            "scale_cloud_replicas：扩容云端缓解排队",
            "route_to_local_edge：把可本地化的请求下沉边端",
            "throttle_low_priority：限流低优请求保核心 SLA",
        ],
    },
}


def _print_manual(kind: str) -> None:
    kb = KB.get(kind)
    if not kb:
        print(f"未知故障类型: {kind}")
        return
    print(f"\n=== 故障排查手册：{kind} ===")
    print(f"[现象] {kb['symptom']}")
    print("[取证]")
    for e in kb["evidence"]:
        print(f"  - {e}")
    print("[假设与验证]")
    for h in kb["hypotheses"]:
        print(f"  - {h}")
    print(f"[根因] {kb['root_cause']}")
    print("[处置]")
    for r in kb["remediation"]:
        print(f"  - {r}")


def _live_triage(base: str = "http://127.0.0.1:8000") -> None:
    try:
        nodes = json.loads(urllib.request.urlopen(f"{base}/api/nodes", timeout=5).read())
        inc = json.loads(urllib.request.urlopen(f"{base}/api/incidents", timeout=5).read())
    except Exception as e:
        print(f"无法连接本地系统（{base}）：{e}")
        return
    print(f"\n=== 实时巡检 @ {base} ===")
    for n in nodes:
        flag = ""
        m = n["metrics"]
        if n["phase"] in ("Offline", "NotReady"):
            flag = " ⚠ 节点不可达"
        elif (m.get("gpu_mem_percent") or 0) > 92:
            flag = " ⚠ 显存打满"
        elif m.get("inf_p95_ms", 0) > 1000 or m.get("net_rtt_ms", 0) > 300:
            flag = " ⚠ 时延超 SLA"
        print(f"  - {n['node_id']} [{n['phase']}] gpu_mem={m.get('gpu_mem_percent')} "
              f"p95={m.get('inf_p95_ms')}ms rtt={m.get('net_rtt_ms')}ms{flag}")
    print(f"未结故障: {len(inc['incidents'])}，已执行动作: {len(inc['actions'])}")
    for i in inc["incidents"]:
        if i["status"] == "open":
            print(f"  - [{i['severity']}] {i['kind']} @ {i['node_id']}: {i['message']}")
            if i["kind"] in KB:
                print(f"      建议: {', '.join(KB[i['kind']]['remediation'])}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="EdgeMind 故障排查 CLI")
    p.add_argument("--kind", help="故障类型：heartbeat_lost/gpu_oom/model_load_fail/latency_spike")
    p.add_argument("--list", action="store_true", help="列出支持的故障类型")
    p.add_argument("--live", action="store_true", help="实时巡检本地运行的系统")
    p.add_argument("--base", default="http://127.0.0.1:8000", help="控制面地址")
    args = p.parse_args(argv)

    if args.list:
        print("支持的故障类型：")
        for k in KB:
            print(f"  - {k}")
        return 0
    if args.live:
        _live_triage(args.base)
        return 0
    if args.kind:
        _print_manual(args.kind)
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

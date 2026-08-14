"""云端 HTTP 控制面 + 可视化看板（标准库 http.server，零依赖）。

对外接口：
- GET  /                     简单可视化看板（自动刷新）
- GET  /api/status           集群总览
- GET  /api/nodes            边缘节点列表（相位/指标）
- GET  /api/incidents        故障事件 + 已执行修复动作
- POST /api/fault            注入故障（演示用），body: {"node_id","kind"}
- POST /v1/chat/completions  OpenAI 兼容接口，转发到 vLLM 网关

这是「云边协同推理」与「可观测/自愈」的统一入口，也便于把项目
接到 K8s Ingress / Dashboard（见 deploy/k8s）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from common.config import CONFIG
from common.logging_setup import get_logger
from cloud.controller import Controller
from cloud.cloudhub import CloudHub
from cloud.vllm_gateway import VLLMGateway, ChatMessage

logger = get_logger("cloud.api", "cloud")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 这些在实例上由 server 注入
    controller: Controller
    cloudhub: CloudHub
    vllm: VLLMGateway

    def _send(self, code: int, body: str, content_type: str = "application/json") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(200, json.dumps(self._status(), ensure_ascii=False))
        elif self.path == "/api/nodes":
            self._send(200, json.dumps(self._nodes(), ensure_ascii=False))
        elif self.path == "/api/incidents":
            self._send(200, json.dumps(self._incidents(), ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}
        if self.path == "/api/fault":
            self._send(200, json.dumps(self._inject_fault(payload), ensure_ascii=False))
        elif self.path == "/v1/chat/completions":
            self._send(200, json.dumps(self._chat(payload), ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    # ---- 数据组装 ----
    def _status(self) -> dict:
        nodes = self.controller.get_nodes()
        ready = sum(1 for n in nodes if n.phase.value in ("Running",))
        return {
            "nodes_total": len(nodes),
            "nodes_ready": ready,
            "open_incidents": len(self.controller.open_incidents()),
            "vllm_mode": "real" if self.vllm.base_url else "mock",
            "vllm_model": self.vllm.model,
        }

    def _nodes(self) -> list:
        out = []
        for n in self.controller.get_nodes():
            m = n.last_metrics
            out.append({
                "node_id": n.node_id,
                "region": n.region,
                "gpu": n.gpu,
                "phase": n.phase.value,
                "heartbeat_age_s": round(n.heartbeat_age(), 1),
                "pending_workloads": n.pending_workloads,
                "metrics": m.to_dict(),
            })
        return out

    def _incidents(self) -> dict:
        incs = [
            {
                "incident_id": i.incident_id,
                "node_id": i.node_id,
                "kind": i.kind,
                "severity": i.severity,
                "message": i.message,
                "status": i.context.get("status", "open"),
                "resolution": i.context.get("resolution", ""),
            }
            for i in self.controller.incidents
        ]
        acts = [
            {"incident_id": a.incident_id, "action": a.action, "detail": a.detail}
            for a in self.controller.actions
        ]
        return {"incidents": incs, "actions": acts}

    def _inject_fault(self, payload: dict) -> dict:
        node_id = payload.get("node_id", "")
        kind = payload.get("kind", "")
        if not node_id or not kind:
            return {"ok": False, "error": "need node_id and kind"}
        # 通过 CloudHub 向边端下发注入故障指令；边端据此模拟异常
        self.cloudhub.send(node_id, json.dumps({
            "type": "command", "node_id": node_id,
            "payload": {"command": "inject_fault", "params": {"kind": kind}},
        }, ensure_ascii=False))
        logger.info("注入故障: node=%s kind=%s", node_id, kind)
        return {"ok": True, "node_id": node_id, "kind": kind}

    def _chat(self, payload: dict) -> dict:
        messages = [ChatMessage(role=m["role"], content=m["content"])
                    for m in payload.get("messages", [])]
        reply = self.vllm.chat(messages)
        return {
            "model": self.vllm.model,
            "choices": [{"message": {"role": "assistant", "content": reply}}],
        }

    def log_message(self, *args):  # 静默默认访问日志
        pass


def start_api(controller: Controller, cloudhub: CloudHub, vllm: VLLMGateway) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((CONFIG.cloud_http_host, CONFIG.cloud_http_port), _Handler)
    server.controller = controller   # type: ignore[attr-defined]
    server.cloudhub = cloudhub       # type: ignore[attr-defined]
    server.vllm = vllm               # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("HTTP 控制面监听 http://%s:%s", CONFIG.cloud_http_host, CONFIG.cloud_http_port)
    return server


DASHBOARD_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>EdgeMind 控制台</title>
<meta http-equiv="refresh" content="3">
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e6edf3;margin:0;padding:24px}
 h1{font-size:20px;margin:0 0 12px}
 .card{background:#161c2c;border:1px solid #243049;border-radius:10px;padding:16px;margin-bottom:16px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #243049}
 .pill{padding:2px 8px;border-radius:999px;font-size:12px}
 .run{background:#1f6f43;color:#d7ffe9}.nr{background:#8a6d00;color:#fff4cf}
 .off{background:#7a1f2b;color:#ffd9df}.pen{background:#394150;color:#cdd6e3}
 .crit{color:#ff7b86}.warn{color:#ffd479}.ok{color:#7ee2a8}
 code{background:#0b0f18;padding:1px 5px;border-radius:4px}
</style></head><body>
<h1>EdgeMind · 云边协同大模型推理与自愈平台</h1>
<div class="card" id="status"></div>
<div class="card"><b>边缘节点</b><div id="nodes"></div></div>
<div class="card"><b>故障与自愈动作</b><div id="inc"></div></div>
<script>
async function load(){
 const s=await (await fetch('/api/status')).json();
 document.getElementById('status').innerHTML=
   `节点 ${s.nodes_ready}/${s.nodes_total} Ready ｜ 未结故障 ${s.open_incidents} ｜ vLLM(${s.vllm_mode}:${s.vllm_model})`;
 const ns=await (await fetch('/api/nodes')).json();
 let h='<table><tr><th>节点</th><th>区域</th><th>GPU</th><th>相位</th><th>心跳 age</th><th>GPU显存</th><th>P95</th></tr>';
 for(const n of ns){const ph=n.phase;const cls=ph==='Running'?'run':ph==='NotReady'?'nr':ph==='Offline'?'off':'pen';
  const gm=n.metrics.gpu_mem_percent;h+=`<tr><td>${n.node_id}</td><td>${n.region}</td><td>${n.gpu}</td>`+
  `<td><span class="pill ${cls}">${ph}</span></td><td>${n.heartbeat_age_s}s</td>`+
  `<td>${gm==null?'-':gm+'%'}</td><td>${n.metrics.inf_p95_ms}ms</td></tr>`;}
 h+='</table>';document.getElementById('nodes').innerHTML=h;
 const inc=await (await fetch('/api/incidents')).json();
 let ih='<table><tr><th>事件</th><th>节点</th><th>级别</th><th>状态</th><th>已执行动作</th></tr>';
 for(const i of inc.incidents){const sev=i.severity==='critical'?'crit':i.severity==='warning'?'warn':'ok';
  ih+=`<tr><td>${i.kind}</td><td>${i.node_id}</td><td class="${sev}">${i.severity}</td>`+
  `<td>${i.status}</td><td>${i.resolution||''}</td></tr>`;}
 for(const a of inc.actions){ih+=`<tr><td colspan="5" style="color:#9fb3c8">↳ [${a.incident_id}] <code>${a.action}</code> ${a.detail}</td></tr>`;}
 ih+='</table>';document.getElementById('inc').innerHTML=ih;
}
load();setInterval(load,3000);
</script></body></html>"""

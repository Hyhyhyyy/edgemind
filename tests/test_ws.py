import os
import sys
import time

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from common.ws import WSServer, WSClient

PORT = 9911
RECEIVED = []


def test_ws_send_recv():
    def on_server(conn, text):
        # 回显，验证双向帧封装/去掩码正确
        conn.send(text)

    def on_client(conn, text):
        RECEIVED.append(text)

    server = WSServer("127.0.0.1", PORT, on_message=on_server)
    server.start()
    time.sleep(0.3)

    client = WSClient(f"ws://127.0.0.1:{PORT}", on_message=on_client)
    client.connect()
    # 等待握手完成
    for _ in range(50):
        if client.connected:
            break
        time.sleep(0.1)
    assert client.connected, "WebSocket 握手失败"

    msg = '{"type":"heartbeat","node_id":"edge-1","payload":{"a":1}}'
    client.send(msg)
    for _ in range(50):
        if RECEIVED:
            break
        time.sleep(0.1)
    assert RECEIVED, "未收到回显消息"
    assert RECEIVED[0] == msg, RECEIVED[0]

    # 长消息（>125 字节，触发 Extended Payload Length）也能正确收发
    long_msg = "x" * 5000
    RECEIVED.clear()
    client.send(long_msg)
    for _ in range(50):
        if RECEIVED:
            break
        time.sleep(0.1)
    assert RECEIVED and RECEIVED[0] == long_msg, "长消息收发失败"

    client.close()
    server.stop()
    print("PASS test_ws_send_recv")


if __name__ == "__main__":
    test_ws_send_recv()

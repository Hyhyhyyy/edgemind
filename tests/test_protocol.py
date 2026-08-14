import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from common.protocol import Envelope, MsgType


def test_envelope_roundtrip():
    e = Envelope.build(MsgType.HEARTBEAT, "edge-1", {"cpu": 12.5, "healthy": True})
    s = e.to_json()
    e2 = Envelope.from_json(s)
    assert e2.type == "heartbeat", e2.type
    assert e2.node_id == "edge-1"
    assert e2.payload == {"cpu": 12.5, "healthy": True}
    print("PASS test_envelope_roundtrip")


if __name__ == "__main__":
    test_envelope_roundtrip()

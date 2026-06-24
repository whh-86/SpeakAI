import websocket
import json

url = "ws://127.0.0.1:5001/ws/asr"
print("Connecting to:", url)
try:
    ws = websocket.create_connection(url, timeout=10)
    print("Connected! Waiting for message...")
    msg = ws.recv()
    print("Received:", msg)
    ws.close()
except Exception as e:
    print("FAILED:", type(e).__name__, e)

"""Dummy local Responses provider for codex-hook-trust-e2e.ts: replays shell commands, no network.

usage: mock_responses_provider.py '<json list of commands>' <n_commands> <tool name> [port]

Port 0 binds a free loopback port; the bound port is printed as "ready <port>" once listening.
"""

import http.server
import json
import sys

COMMANDS = json.loads(sys.argv[1])
LIMIT = int(sys.argv[2])
TOOL = sys.argv[3]
PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 18080


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}"
        )
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def emit(kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        emit("response.created", {"type": "response.created", "response": {"id": "r1"}})
        done = len(
            [i for i in body["input"] if i.get("type") == "function_call_output"]
        )
        if done >= LIMIT:
            item = {
                "type": "message",
                "role": "assistant",
                "id": "m1",
                "content": [{"type": "output_text", "text": "done"}],
            }
        else:
            arg = (
                {"cmd": COMMANDS[done]}
                if TOOL == "exec_command"
                else {"command": COMMANDS[done]}
            )
            item = {
                "type": "function_call",
                "id": f"fc{done}",
                "call_id": f"call{done}",
                "name": TOOL,
                "arguments": json.dumps(arg),
            }
        emit(
            "response.output_item.done",
            {"type": "response.output_item.done", "item": item},
        )
        usage = {
            "input_tokens": 1,
            "input_tokens_details": None,
            "output_tokens": 1,
            "output_tokens_details": None,
            "total_tokens": 2,
        }
        emit(
            "response.completed",
            {"type": "response.completed", "response": {"id": "r1", "usage": usage}},
        )


server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
print(f"ready {server.server_address[1]}", flush=True)
server.serve_forever()

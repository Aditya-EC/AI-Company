"""TEST FIXTURE ONLY: a scripted HTTP server that speaks the Ollama and OpenAI chat APIs.

It lets tests exercise the real adapters over real HTTP without a model. It is never used by
the application at runtime, and its responses are not presented as real AI output.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLLMServer:
    def __init__(self):
        self.script = []          # list of (status, body_dict_or_text) consumed per chat call
        self.default = None       # used when the script is empty
        self.requests = []
        self.tags = ["fake-model:latest"]
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                data = body if isinstance(body, (bytes, str)) else json.dumps(body)
                data = data.encode() if isinstance(data, str) else data
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._send(200, {"models": [{"name": n} for n in server.tags]})
                if self.path.endswith("/models"):
                    if self.headers.get("Authorization") != "Bearer good-key":
                        return self._send(401, {"error": "invalid api key"})
                    return self._send(200, {"data": [{"id": "x"}]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                server.requests.append({"path": self.path, "json": payload, "auth": self.headers.get("Authorization")})
                status, body = server.script.pop(0) if server.script else server.default
                if callable(body):
                    body = body(payload)
                self._send(status, body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # helpers producing provider-shaped responses
    @staticmethod
    def ollama(content, pin=12, pout=30):
        return (200, {"message": {"role": "assistant", "content": content}, "prompt_eval_count": pin, "eval_count": pout, "done": True})

    @staticmethod
    def openai(content):
        return (200, {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 10, "completion_tokens": 20}})

    @staticmethod
    def final(output, summary="done"):
        return json.dumps({"action": "final", "output": output, "summary": summary})

    @staticmethod
    def tool(slug, data):
        return json.dumps({"action": "tool", "tool": slug, "input": data, "reason": "test"})

import json
import time
import urllib.error
import urllib.request

from common import env_values, paths


def probe(key, model, label):
    payload = {"model": model, "enable_thinking": False, "max_tokens": 64,
               "messages": [{"role": "user", "content": "Return JSON with ok true."}],
               "response_format": {"type": "json_object"}}
    request = urllib.request.Request(
        "https://api.siliconflow.cn/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    status, code = None, None
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            status = response.status
            response.read(4096)
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            body = json.loads(error.read(4096))
            code = body.get("code")
            if code is None and isinstance(body.get("error"), dict):
                code = body["error"].get("code")
            if isinstance(code, str) and code.isdigit():
                code = int(code)
            if not isinstance(code, int) or isinstance(code, bool):
                code = None
        except (ValueError, AttributeError):
            pass
    except (OSError, urllib.error.URLError):
        pass
    result = {"status": status, "latency_ms": round((time.monotonic() - started) * 1000),
              "provider_code": code,
              "summary": f"chat_probe_{label}_" + ("success" if status == 200 else "failed")}
    print(json.dumps(result), flush=True)
    return status


if __name__ == "__main__":
    environment = env_values(paths("bwg")[2])
    key = environment.get("SILICONFLOW_KEY") or environment.get("SILICONFLOW_API_KEY")
    if not key:
        raise SystemExit("configured provider credential missing")
    if probe(key, "Qwen/Qwen3.5-9B", "9b") == 503:
        probe(key, "Qwen/Qwen3.5-4B", "4b")

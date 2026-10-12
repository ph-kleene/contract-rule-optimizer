"""Model boundary: a dependency-free HTTP adapter and an explicit offline demo."""

import json
import os
import re
from typing import Protocol
from urllib import error, parse, request

Message = dict[str, str]


class Model(Protocol):
    def complete(self, stage: str, messages: list[Message]) -> str:
        """Return text containing a single JSON object."""


class ModelError(RuntimeError):
    """A sanitized failure, without raw provider response or credentials."""


def parse_object(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise ValueError("incomplete JSON code fence")
        raw = "\n".join(lines[1:-1])
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("model output must be a JSON object")
    return value


class ChatCompletionsModel:
    """Call a service implementing the documented chat/completions JSON shape."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60):
        parsed = parse.urlparse(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not local_http) or not parsed.netloc:
            raise ValueError("LLM_BASE_URL must use HTTPS (HTTP is allowed on localhost)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LLM_BASE_URL must not contain credentials, query, or fragment")
        if not api_key.strip() or not model.strip():
            raise ValueError("LLM_API_KEY and LLM_MODEL must be set")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> "ChatCompletionsModel":
        values = [os.getenv(name, "") for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")]
        if not all(values):
            raise ValueError("set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL; or use --mock")
        return cls(*values)

    def complete(self, stage: str, messages: list[Message]) -> str:
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0},
                          ensure_ascii=False).encode("utf-8")
        req = request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        # Do not forward authorization to a redirected endpoint.
        class NoRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        try:
            with request.build_opener(NoRedirect).open(req, timeout=self.timeout) as response:
                result = json.load(response)
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ModelError("empty_model_response")
            return content
        except error.HTTPError as exc:
            raise ModelError(f"http_status_{exc.code}") from None
        except (error.URLError, TimeoutError, OSError):
            raise ModelError("transport_error") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("invalid_provider_response") from None


class DemoModel:
    """Synthetic, deterministic fixtures; this does not evaluate legal meaning."""

    def complete(self, stage: str, messages: list[Message]) -> str:
        user = messages[1]["content"]
        if stage == "polish":
            original = re.search(r"<待优化规则>\n(.*?)\n</待优化规则>", user, re.S)
            if original is None:
                raise ModelError("invalid_demo_input")
            result = {"rule_statement": "审查时应检查：" + original.group(1), "parameters": {}}
        elif stage == "judge":
            result = {"accepted": True, "reason": "离线示例固定通过，不代表质量评估。"}
        elif stage == "validate":
            match = re.search(r"<当前规则列表>\n(.*?)\n</当前规则列表>", user, re.S)
            if match is None:
                raise ModelError("invalid_demo_input")
            rows = json.loads(match.group(1))
            # A deliberately narrow fixture to make the sample trace readable.
            # Only this repository's synthetic payment example is changed.
            output = []
            for row in rows:
                statement = row["current_statement"]
                changed = (row["rule_id"] == "DEMO-PAYMENT" and "45日" in user and "30日" in statement)
                if changed:
                    statement = statement.replace("30日", "30至45日")
                output.append({"rule_id": row["rule_id"], "statement": statement,
                               "changed": changed,
                               "change_reason": "合成示例固定替换付款期限。" if changed else ""})
            result = {"rules": output, "summary": "离线模拟完成；未调用模型。"}
        else:
            raise ModelError("unknown_stage")
        return json.dumps(result, ensure_ascii=False)

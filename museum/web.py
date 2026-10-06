"""极简 Web 框架：路由与响应（仅标准库）。"""
import json
import re


class Response:
    def __init__(self, body="", status=200,
                 content_type="text/html; charset=utf-8", headers=None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.content_type = content_type
        self.headers = headers or {}


def json_response(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False, indent=2),
                    status, "application/json; charset=utf-8")


def redirect(location):
    return Response("", 303, headers={"Location": location})


class Router:
    def __init__(self):
        self.routes = []

    def route(self, method, pattern):
        def deco(fn):
            self.add(method, pattern, fn)
            return fn
        return deco

    def add(self, method, pattern, fn):
        regex = re.compile(
            "^" + re.sub(r"<(\w+)>", lambda m: f"(?P<{m.group(1)}>[^/]+)", pattern) + "$"
        )
        self.routes.append((method.upper(), regex, fn))

    def dispatch(self, method, path):
        for m, regex, fn in self.routes:
            if m != method.upper():
                continue
            match = regex.match(path)
            if match:
                return fn, match.groupdict()
        return None, None

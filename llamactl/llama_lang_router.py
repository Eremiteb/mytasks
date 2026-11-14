#!/usr/bin/env python3
"""Прокси перед роутером llama.cpp с виртуальной моделью «auto».

Запрос с моделью «auto» (или без поля model) уходит в казахскую модель, если
последнее сообщение пользователя написано по-казахски, иначе — в модель по
умолчанию. Явно выбранная модель не меняется. Остальные запросы (веб-интерфейс,
/v1/models, /models/sse и т.п.) передаются роутеру как есть; в списки моделей
добавляется «auto» — копия записи модели по умолчанию.

Настройки — переменные окружения (значения по умолчанию в скобках):
  LANG_ROUTER_LISTEN         адрес:порт прокси (127.0.0.1:8081)
  LANG_ROUTER_UPSTREAM       роутер llama.cpp (http://127.0.0.1:8080)
  LANG_ROUTER_KK_MODEL       модель для казахского (qwen3.5-9b-kazakh)
  LANG_ROUTER_DEFAULT_MODEL  модель для остального (qwen3.6-35b-a3b)
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUTO = "auto"
# Буквы, которых нет в русском алфавите: по ним казахский текст отличается от русского
KK_LETTERS = frozenset("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
# Доля казахских букв среди кириллических, начиная с которой текст считается казахским.
# В казахском тексте их обычно 5–10 %, в русском с одним казахским названием — меньше.
KK_MIN_SHARE = 0.03
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                         "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host"})
MODEL_LIST_PATHS = frozenset({"/models", "/v1/models"})


def message_text(content: object) -> str:
    """Текст сообщения: строка или части OpenAI-формата [{type: text, text: …}, …]."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def is_kazakh(text: str) -> bool:
    cyr = [ch for ch in text if "Ѐ" <= ch <= "ӿ"]
    kk = sum(ch in KK_LETTERS for ch in cyr)
    return kk > 0 and kk / len(cyr) >= KK_MIN_SHARE


def last_user_text(body: dict) -> str:
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return message_text(msg.get("content"))
    prompt = body.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def choose_model(body: dict, kk_model: str, default_model: str) -> str | None:
    """Новая модель для запроса или None, если модель выбрана явно."""
    model = body.get("model")
    if model not in (None, "", AUTO):
        return None
    return kk_model if is_kazakh(last_user_text(body)) else default_model


class ModelMasker:
    """Заменяет в потоке ответа "model":"<реальная>" на "model":"auto", чтобы
    веб-интерфейс не переключал выбор с auto на модель из ответа. Хвост чанка
    придерживается, чтобы не пропустить шаблон, разрезанный границей чанков."""

    def __init__(self, routed: str) -> None:
        self.pairs = [(f'"model":"{routed}"'.encode(), f'"model":"{AUTO}"'.encode()),
                      (f'"model": "{routed}"'.encode(), f'"model": "{AUTO}"'.encode())]
        self.keep = max(len(src) for src, _ in self.pairs) - 1
        self.tail = b""

    def feed(self, chunk: bytes) -> bytes:
        data = self.tail + chunk
        for src, dst in self.pairs:
            data = data.replace(src, dst)
        cut = max(len(data) - self.keep, 0)
        self.tail = data[cut:]
        return data[:cut]

    def flush(self) -> bytes:
        out, self.tail = self.tail, b""
        return out


def add_auto_entry(listing: dict, default_model: str) -> dict:
    """Добавляет в список моделей запись «auto» — копию модели по умолчанию."""
    data = listing.get("data")
    if not isinstance(data, list) or any(isinstance(m, dict) and m.get("id") == AUTO for m in data):
        return listing
    base = next((m for m in data if isinstance(m, dict) and m.get("id") == default_model), None)
    if base is None:
        return listing
    entry = json.loads(json.dumps(base))
    entry["id"] = AUTO
    entry["aliases"] = []
    # «Загружена» всегда: иначе веб-интерфейс при выборе auto заранее грузит модель
    # по умолчанию, а казахский запрос тут же её выгружает. Реальная модель
    # загрузится по первому запросу, когда известен язык.
    if isinstance(entry.get("status"), dict):
        entry["status"]["value"] = "loaded"
    data.insert(0, entry)
    return listing


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream: urllib.parse.SplitResult
    kk_model: str
    default_model: str

    def log_message(self, fmt: str, *args: object) -> None:  # журнал — только маршрутизация и ошибки
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _rewrite(self, path: str, body: bytes) -> tuple[str, bytes, str | None, bool]:
        """Подменяет «auto»: в JSON-теле (по языку для чатов) и в ?model= (модель по умолчанию).
        Возвращает путь, тело, выбранную модель и признак «чат через auto» (маскировать ответ)."""
        routed = None
        masked = False
        parts = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if any(k == "model" and v == AUTO for k, v in query):
            query = [(k, self.default_model if k == "model" and v == AUTO else v) for k, v in query]
            path = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
            routed = self.default_model
        if body and (self.headers.get("Content-Type") or "").startswith("application/json"):
            try:
                data = json.loads(body)
            except ValueError:
                return path, body, routed, masked
            if isinstance(data, dict):
                chat = parts.path.endswith(("/chat/completions", "/completions"))
                if chat:
                    model = choose_model(data, self.kk_model, self.default_model)
                    masked = model is not None
                elif data.get("model") == AUTO:
                    model = self.default_model
                else:
                    model = None
                if model:
                    data["model"] = model
                    body = json.dumps(data, ensure_ascii=False).encode()
                    routed = model
        return path, body, routed, masked

    def _auto_load_noop(self, body: bytes) -> bool:
        """POST /models/load|unload для auto отвечает сам: у auto нет своей модели."""
        if self.command != "POST" or urllib.parse.urlsplit(self.path).path not in ("/models/load", "/models/unload"):
            return False
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return False
        if not isinstance(data, dict) or data.get("model") != AUTO:
            return False
        payload = b'{"success":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return True

    def _proxy(self) -> None:
        body = self._body()
        if self._auto_load_noop(body):
            return
        path, body, routed, masked = self._rewrite(self.path, body)
        if routed:
            sys.stderr.write(f"{self.command} {urllib.parse.urlsplit(path).path} → {routed}\n")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        if body:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection(self.upstream.hostname, self.upstream.port, timeout=3600)
        try:
            conn.request(self.command, path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            self.send_error(502, f"роутер llama.cpp недоступен: {e}")
            return
        try:
            self._relay(resp, urllib.parse.urlsplit(path).path, routed if masked else None)
        except (BrokenPipeError, ConnectionResetError):
            pass  # клиент закрыл соединение (отмена генерации)
        finally:
            conn.close()

    def _relay(self, resp: http.client.HTTPResponse, path: str, routed: str | None = None) -> None:
        ctype = resp.getheader("Content-Type") or ""
        masker = ModelMasker(routed) if routed else None
        if self.command == "GET" and path in MODEL_LIST_PATHS and ctype.startswith("application/json"):
            payload = resp.read()
            try:
                payload = json.dumps(add_auto_entry(json.loads(payload), self.default_model)).encode()
            except ValueError:
                pass
            self.send_response(resp.status, resp.reason)
            self._copy_headers(resp)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(resp.status, resp.reason)
        self._copy_headers(resp)
        if routed:
            self.send_header("X-Routed-Model", routed)
        if resp.getheader("Content-Length") is not None:
            payload = resp.read()
            if masker:
                payload = masker.feed(payload) + masker.flush()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        # Потоковый ответ (SSE): пересылаем частями по мере поступления
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while chunk := resp.read1(65536):
            self._write_chunk(masker.feed(chunk) if masker else chunk)
        if masker:
            self._write_chunk(masker.flush())
        self.wfile.write(b"0\r\n\r\n")

    def _write_chunk(self, chunk: bytes) -> None:
        if chunk:  # пустой чанк означал бы конец ответа
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()

    def _copy_headers(self, resp: http.client.HTTPResponse) -> None:
        for k, v in resp.getheaders():
            if k.lower() not in HOP_HEADERS:
                self.send_header(k, v)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _proxy


def main() -> None:
    host, _, port = os.environ.get("LANG_ROUTER_LISTEN", "127.0.0.1:8081").rpartition(":")
    Handler.upstream = urllib.parse.urlsplit(os.environ.get("LANG_ROUTER_UPSTREAM", "http://127.0.0.1:8080"))
    Handler.kk_model = os.environ.get("LANG_ROUTER_KK_MODEL", "qwen3.5-9b-kazakh")
    Handler.default_model = os.environ.get("LANG_ROUTER_DEFAULT_MODEL", "qwen3.6-35b-a3b")
    if Handler.upstream.hostname is None or Handler.upstream.port is None:
        sys.exit("LANG_ROUTER_UPSTREAM должен быть вида http://хост:порт")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    server.daemon_threads = True
    sys.stderr.write(f"llama-lang-router: {host}:{port} → {Handler.upstream.geturl()} "
                     f"(auto: казахский → {Handler.kk_model}, иначе → {Handler.default_model})\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

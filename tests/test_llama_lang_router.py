"""Тесты прокси llama_lang_router: определение языка, выбор модели, список моделей,
сквозная маршрутизация через локальный фиктивный роутер."""
import http.client
import http.server
import importlib.util
import json
import pathlib
import threading
import unittest

SPEC = importlib.util.spec_from_file_location(
    "llama_lang_router", pathlib.Path(__file__).resolve().parent.parent / "llamactl" / "llama_lang_router.py")
assert SPEC and SPEC.loader
lr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lr)

KK, DEF = "qwen3.5-9b-kazakh", "qwen3.6-35b-a3b"


class LanguageTest(unittest.TestCase):
    def test_kazakh_text(self):
        self.assertTrue(lr.is_kazakh("Наурыз мейрамы қай күндері тойланады?"))

    def test_russian_text(self):
        self.assertFalse(lr.is_kazakh("Когда отмечают Наурыз в Казахстане?"))

    def test_russian_with_one_kazakh_name_in_long_text(self):
        text = "Подскажи, пожалуйста, как лучше всего добраться на поезде до города Ақтөбе из Алматы зимой"
        self.assertFalse(lr.is_kazakh(text))

    def test_non_cyrillic(self):
        self.assertFalse(lr.is_kazakh("When is Nauryz?"))
        self.assertFalse(lr.is_kazakh(""))


class ChooseModelTest(unittest.TestCase):
    def body(self, model, text):
        return {"model": model, "messages": [{"role": "system", "content": "Сен көмекшісің."},
                                             {"role": "user", "content": text}]}

    def test_auto_kazakh(self):
        self.assertEqual(lr.choose_model(self.body("auto", "Сәлем, қалың қалай?"), KK, DEF), KK)

    def test_auto_russian(self):
        self.assertEqual(lr.choose_model(self.body("auto", "Привет, как дела?"), KK, DEF), DEF)

    def test_missing_model(self):
        body = self.body(None, "Сәлем, қалың қалай?")
        del body["model"]
        self.assertEqual(lr.choose_model(body, KK, DEF), KK)

    def test_explicit_model_untouched(self):
        self.assertIsNone(lr.choose_model(self.body("deepseek-v4-pro-qwen3.5-9b", "Сәлем, қалың қалай?"), KK, DEF))

    def test_multipart_content(self):
        body = {"model": "auto", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Суреттегі мәтінді көшіріп жаз"}, {"type": "image_url", "image_url": {"url": "x"}}]}]}
        self.assertEqual(lr.choose_model(body, KK, DEF), KK)

    def test_last_user_message_wins(self):
        body = {"model": "auto", "messages": [{"role": "user", "content": "Сәлем, қалың қалай?"},
                                              {"role": "assistant", "content": "Жақсы."},
                                              {"role": "user", "content": "Теперь ответь по-русски"}]}
        self.assertEqual(lr.choose_model(body, KK, DEF), DEF)


class ModelListTest(unittest.TestCase):
    def test_auto_added_as_copy_of_default(self):
        listing = {"data": [{"id": KK, "aliases": []}, {"id": DEF, "aliases": ["x"], "status": {"value": "loaded"}}]}
        listing["data"][1]["status"]["value"] = "unloaded"
        out = lr.add_auto_entry(listing, DEF)
        self.assertEqual(out["data"][0]["id"], "auto")
        # auto всегда «загружена», модель по умолчанию — как есть
        self.assertEqual(out["data"][0]["status"], {"value": "loaded"})
        self.assertEqual(out["data"][2]["status"], {"value": "unloaded"})
        self.assertEqual(out["data"][0]["aliases"], [])
        self.assertEqual(out["data"][2]["id"], DEF)

    def test_no_default_no_change(self):
        listing = {"data": [{"id": KK}]}
        self.assertEqual(lr.add_auto_entry(listing, DEF), {"data": [{"id": KK}]})


class MaskerTest(unittest.TestCase):
    def test_pattern_split_across_chunks(self):
        m = lr.ModelMasker(KK)
        src = f'data: {{"model":"{KK}","x":1}}\n\ndata: {{"model": "{KK}"}}\n\n'.encode()
        out = b"".join(m.feed(src[i:i + 7]) for i in range(0, len(src), 7)) + m.flush()
        self.assertEqual(out, b'data: {"model":"auto","x":1}\n\ndata: {"model": "auto"}\n\n')

    def test_other_text_untouched(self):
        m = lr.ModelMasker(KK)
        self.assertEqual(m.feed(b'{"model":"other"}') + m.flush(), b'{"model":"other"}')


class FakeRouter(http.server.BaseHTTPRequestHandler):
    """Фиктивный роутер: отвечает моделью из запроса и отдаёт список моделей."""

    def log_message(self, *args):
        return

    def do_GET(self):
        payload = json.dumps({"data": [{"id": DEF}, {"id": KK}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = json.dumps({"model": body.get("model")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        handler = type("H", (lr.Handler,), {})
        handler.upstream = lr.urllib.parse.urlsplit(f"http://127.0.0.1:{cls.upstream.server_address[1]}")
        handler.kk_model, handler.default_model = KK, DEF
        cls.proxy = lr.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.upstream.shutdown()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_address[1], timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        resp = conn.getresponse()
        out = json.loads(resp.read())
        routed = resp.getheader("X-Routed-Model")
        conn.close()
        return out, routed

    def test_chat_routed_by_language(self):
        kk, kk_routed = self.request("POST", "/v1/chat/completions", {
            "model": "auto", "messages": [{"role": "user", "content": "Қазақстанның астанасы қай қала?"}]})
        ru, ru_routed = self.request("POST", "/v1/chat/completions", {
            "model": "auto", "messages": [{"role": "user", "content": "Столица Казахстана?"}]})
        self.assertEqual((kk_routed, ru_routed), (KK, DEF))
        # В ответе остаётся auto, чтобы веб-интерфейс не переключал выбор модели
        self.assertEqual((kk["model"], ru["model"]), ("auto", "auto"))

    def test_explicit_model_passes(self):
        out, routed = self.request("POST", "/v1/chat/completions",
                                   {"model": KK, "messages": [{"role": "user", "content": "Столица Казахстана?"}]})
        self.assertEqual((out["model"], routed), (KK, None))

    def test_load_auto_answered_locally(self):
        out, _ = self.request("POST", "/models/load", {"model": "auto"})
        self.assertEqual(out, {"success": True})

    def test_load_real_model_forwarded(self):
        out, _ = self.request("POST", "/models/load", {"model": KK})
        self.assertEqual(out, {"model": KK})  # ответ фиктивного роутера

    def test_models_list_has_auto(self):
        out, _ = self.request("GET", "/v1/models")
        self.assertEqual([m["id"] for m in out["data"]], ["auto", DEF, KK])


if __name__ == "__main__":
    unittest.main()

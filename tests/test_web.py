import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import AioHTTPTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gmap_tracker import config, web

from test_config import FONTS, VALID

TOKEN = "test-token"


class FakeTask:
    def __init__(self):
        self.current_loop = 4

    def is_running(self):
        return True

    def failed(self):
        return False

    @property
    def next_iteration(self):
        return None


class FakeTracker:
    def __init__(self, cfg):
        self.config = cfg
        self.task = FakeTask()
        self.last_error = None
        self.display = SimpleNamespace(width=128, height=32)
        self.applied = []
        self.fetch_result = {"routes": [{"duration": "1800s", "distanceMeters": 12000}]}
        self.fetch_error: Exception | None = None

    async def apply_config(self, new):
        self.config = new
        self.applied.append(new)

    async def fetch(self):
        if self.fetch_error:
            raise self.fetch_error
        return self.fetch_result


class WebTestCase(AioHTTPTestCase):
    async def get_application(self):
        self.dir = Path(tempfile.mkdtemp())
        self.defaults = self.dir / "defaults.json"
        self.override = self.dir / "config.json"
        self.defaults.write_text(json.dumps(VALID))

        app = web.build_app(TOKEN, self.defaults, self.override)
        self.tracker = FakeTracker(config.parse(VALID, FONTS))
        web.attach(app, self.tracker, None)
        return app

    def auth(self, **extra):
        return {"X-Auth-Token": TOKEN, **extra}


class TestAuth(WebTestCase):
    async def test_index_is_open(self):
        self.assertEqual((await self.client.get("/")).status, 200)

    async def test_api_requires_token(self):
        self.assertEqual((await self.client.get("/api/status")).status, 401)

    async def test_api_rejects_wrong_token(self):
        res = await self.client.get("/api/status", headers={"X-Auth-Token": "nope"})
        self.assertEqual(res.status, 401)

    async def test_api_accepts_correct_token(self):
        self.assertEqual((await self.client.get("/api/status", headers=self.auth())).status, 200)

    async def test_503_until_tracker_attached(self):
        app = web.build_app(TOKEN, self.defaults, self.override)
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            res = await client.get("/api/status", headers=self.auth())
            self.assertEqual(res.status, 503)


class TestConfigEndpoints(WebTestCase):
    async def test_get_config_returns_merged(self):
        body = await (await self.client.get("/api/config", headers=self.auth())).json()
        self.assertEqual(body["config"]["units"], "IMPERIAL")
        self.assertNotIn("X-Goog-Api-Key", json.dumps(body))

    async def test_put_applies_and_persists(self):
        res = await self.client.put("/api/config", headers=self.auth(), json={"units": "METRIC"})
        self.assertEqual(res.status, 200)
        self.assertEqual(self.tracker.config.units, "METRIC")
        self.assertEqual(json.loads(self.override.read_text()), {"units": "METRIC"})
        self.assertEqual(len(self.tracker.applied), 1)

    async def test_put_rejects_invalid_and_does_not_persist(self):
        res = await self.client.put("/api/config", headers=self.auth(), json={"units": "FURLONGS"})
        self.assertEqual(res.status, 400)
        self.assertEqual((await res.json())["field"], "units")
        self.assertFalse(self.override.exists())
        self.assertEqual(self.tracker.applied, [])

    async def test_put_rejects_font_traversal(self):
        res = await self.client.put(
            "/api/config", headers=self.auth(), json={"display": {"font": "../../../etc/shadow"}}
        )
        self.assertEqual(res.status, 400)
        self.assertEqual((await res.json())["field"], "display.font")
        self.assertFalse(self.override.exists())

    async def test_put_rejects_non_object_body(self):
        res = await self.client.put("/api/config", headers=self.auth(), json=[1, 2])
        self.assertEqual(res.status, 400)

    async def test_reset_removes_override(self):
        await self.client.put("/api/config", headers=self.auth(), json={"units": "METRIC"})
        res = await self.client.post("/api/config/reset", headers=self.auth())
        self.assertEqual(res.status, 200)
        self.assertFalse(self.override.exists())
        self.assertEqual((await res.json())["config"]["units"], "IMPERIAL")


class TestStatusAndFonts(WebTestCase):
    async def test_status_shape(self):
        body = await (await self.client.get("/api/status", headers=self.auth())).json()
        self.assertTrue(body["is_running"])
        self.assertTrue(body["configured"])
        self.assertEqual(body["current_loop"], 4)
        self.assertIsNone(body["last_error"])

    async def test_fonts_flags_fit_and_never_leaks_paths(self):
        body = await (await self.client.get("/api/fonts", headers=self.auth())).json()
        names = [f["name"] for f in body["fonts"]]
        self.assertIn("small_6x8", names)
        self.assertNotIn("/", json.dumps(body).replace("\\/", ""))
        fitting = [f for f in body["fonts"] if f["fits"]]
        self.assertTrue(fitting)
        self.assertTrue(all(f["fits"] for f in body["fonts"][: len(fitting)]))

    async def test_oversized_font_is_rejected_with_reason(self):
        body = await (await self.client.get("/api/fonts", headers=self.auth())).json()
        big = next(f for f in body["fonts"] if f["name"] == "grotesk_16x32")
        self.assertFalse(big["fits"])
        self.assertTrue(big["reason"])


class TestRouteTest(WebTestCase):
    async def test_returns_duration(self):
        body = await (await self.client.post("/api/route/test", headers=self.auth())).json()
        self.assertEqual(body["duration"], "1800s")

    async def test_upstream_failure_is_502(self):
        self.tracker.fetch_error = RuntimeError("boom")
        res = await self.client.post("/api/route/test", headers=self.auth())
        self.assertEqual(res.status, 502)
        self.assertIn("boom", (await res.json())["error"])

    async def test_unconfigured_route_is_400(self):
        self.tracker.config = config.replace(self.tracker.config, route=None)
        res = await self.client.post("/api/route/test", headers=self.auth())
        self.assertEqual(res.status, 400)

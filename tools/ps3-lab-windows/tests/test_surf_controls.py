"""Fixture-only CatalogService Surf control contract and desktop call paths."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ps3lab.jellyfin import JellyfinClient, JellyfinError
from ps3lab.surf import (
    SurfError, SurfSelection, SurfSession, SurfStatus, mode_for_item,
    parse_status, surf_url, token_valid, validate_catalog_url,
)
from app import Ps3Lab


TOKEN_A = "a" * 32
TOKEN_B = "b" * 32
TOKEN_C = "c" * 32
CATALOG = "http://catalog.local:8097"


class Reply:
    def __init__(self, payload=None, status=200):
        self.status = status
        self._body = b"" if payload is None else json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self._body

    def getcode(self):
        return self.status


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeClient:
    token = "fixture-token"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def surf_request(self, catalog, mode, action=None, payload=None):
        self.calls.append((catalog, mode, action, payload))
        if not self.responses:
            raise AssertionError("unexpected Surf request")
        return self.responses.pop(0)


class FakeUI:
    def __init__(self):
        self.feedback = []
        self.selections = []
        self.shown = []
        self.hidden = 0

    def set_surf_feedback(self, text, ok=False):
        self.feedback.append((text, ok))

    def set_surf_selection(self, selection):
        self.selections.append(selection)

    def show_surf(self, mode):
        self.shown.append(mode)

    def hide_surf(self):
        self.hidden += 1


class Harness(Ps3Lab):
    def __init__(self, client):
        self.current_item = {"Name": "Surf: All Channels", "Type": "TvChannel"}
        self.surf = SurfSession()
        self.surf_busy = False
        self._surf_request_id = 0
        self._surf_active_request = 0
        self.catalog = Var(CATALOG)
        self.client = client
        self.status = Var()
        self.ui = FakeUI()
        self.outbox = []
        self.events = []
        self.session = object()
        self.running = True

    def worker(self, callback, *args):
        try:
            self.outbox.append(("result", callback(*args)))
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            self.outbox.append(("error", str(exc)))

    def emit(self, name, data=None):
        self.events.append((name, data))

    def player_command(self, *_args, **_kwargs):
        raise AssertionError("Surf controls must not touch player pause/seek")

    def begin(self, *_args, **_kwargs):
        raise AssertionError("Surf selection must not switch playback")

    def seek_to_seconds(self, *_args, **_kwargs):
        raise AssertionError("Surf selection must not seek playback")

    def run_control(self, action):
        before = len(self.outbox)
        self.surf_control(action)
        if len(self.outbox) != before + 1:
            raise AssertionError("expected one completed Surf worker result")
        kind, payload = self.outbox[-1]
        if kind != "result":
            raise AssertionError("Surf worker failed: " + str(payload))
        key, result = payload
        if key != "surf_result":
            raise AssertionError("unexpected worker result " + str(key))
        if result.request_id == self._surf_active_request:
            self._surf_active_request = 0
            self.surf_busy = False
        self.apply_surf_result(result)
        return result


def status_payload(source="Now Playing", entries=None, state="playing"):
    return {
        "source": source, "state": state, "group": "Fixture",
        "source_id": "source-id", "boot": "boot-1",
        "recent_sources": entries or [
            {"source": "Alpha", "token": TOKEN_A},
            {"source": "Beta", "token": TOKEN_B},
            {"source": "Gamma", "token": TOKEN_C},
        ],
    }


class CatalogContractTests(unittest.TestCase):
    def make_client(self):
        client = JellyfinClient("http://jellyfin.local:8096", "ps3-lab-test")
        client.token = "fixture-token"
        client.user_id = "fixture-user"
        return client

    def test_status_and_action_urls_match_native_contract(self):
        client = self.make_client()
        requests = []

        def fake_urlopen(req, timeout=None):
            requests.append((req, timeout))
            return Reply(status_payload())

        with patch("ps3lab.jellyfin.urllib.request.urlopen", fake_urlopen):
            code, payload = client.surf_request(CATALOG, "all")
            self.assertEqual(code, 200)
            self.assertEqual(payload["source"], "Now Playing")
            code, _ = client.surf_request(CATALOG, "sports", "pause", {})
            self.assertEqual(code, 200)

        get_request = requests[0][0]
        self.assertEqual(get_request.full_url, CATALOG + "/api/surf/all")
        self.assertEqual(get_request.get_method(), "GET")
        self.assertIn('Token="fixture-token"', get_request.get_header("Authorization"))
        self.assertEqual(get_request.get_header("User-agent"), "JellyfinPS3/0.1")
        self.assertIsNone(get_request.data)

        post_request = requests[1][0]
        self.assertEqual(post_request.full_url,
                         CATALOG + "/api/surf/sports/pause")
        self.assertEqual(post_request.get_method(), "POST")
        self.assertEqual(json.loads(post_request.data.decode()), {})

    def test_favorite_payloads_use_current_source_or_immutable_previous_token(self):
        client = self.make_client()
        requests = []

        def fake_urlopen(req, timeout=None):
            requests.append(req)
            return Reply({})

        with patch("ps3lab.jellyfin.urllib.request.urlopen", fake_urlopen):
            client.surf_request(CATALOG, "all", "favorite",
                                {"source": "Now Playing", "trace": "trace-1"})
            client.surf_request(CATALOG, "all", "favorite", {
                "source": "Alpha", "previous": True,
                "previous_token": TOKEN_A, "trace": "trace-2",
            })

        self.assertEqual(requests[0].full_url, CATALOG + "/api/surf/all/favorite")
        self.assertEqual(json.loads(requests[0].data.decode()), {
            "source": "Now Playing", "trace": "trace-1"})
        self.assertEqual(json.loads(requests[1].data.decode()), {
            "source": "Alpha", "previous": True,
            "previous_token": TOKEN_A, "trace": "trace-2"})

    def test_http_failure_is_not_reported_as_success(self):
        client = self.make_client()

        def fake_urlopen(req, timeout=None):
            raise HTTPError(req.full_url, 503, "unavailable", {}, None)

        with patch("ps3lab.jellyfin.urllib.request.urlopen", fake_urlopen):
            self.assertEqual(client.surf_request(CATALOG, "all", "favorite", {}),
                             (503, {}))

    def test_catalog_url_and_auth_are_validated(self):
        client = self.make_client()
        for bad in ("ftp://host", "http://user:pass@host",
                    "http://host/?q=1", "http://host/other"):
            with self.subTest(url=bad):
                with self.assertRaises(SurfError):
                    validate_catalog_url(bad)
                with self.assertRaises(SurfError):
                    surf_url(bad, "all")
        client.token = ""
        with self.assertRaises(JellyfinError):
            client.surf_request(CATALOG, "all")

    def test_mode_mapping_and_tokens_are_exact(self):
        self.assertEqual(mode_for_item({"Name": "Surf: All Channels"}), "all")
        self.assertEqual(mode_for_item({"Name": "Surf: Sports"}), "sports")
        self.assertEqual(mode_for_item({"Name": "Surf: Movies & Series"}),
                         "movies-series")
        self.assertEqual(mode_for_item({"Name": "surf: sports"}), "")
        self.assertEqual(mode_for_item(None), "")
        self.assertTrue(token_valid(TOKEN_A))
        for bad in ("a" * 31, "g" * 32, "", None, "a" * 33):
            self.assertFalse(token_valid(bad))
        with self.assertRaises(SurfError):
            SurfSelection("Alpha", "not-a-token")

    def test_status_parser_ignores_invalid_history_entries(self):
        payload = status_payload(entries=[
            {"source": "Alpha", "token": TOKEN_A},
            {"source": "", "token": TOKEN_B},
            {"source": "Bad", "token": "short"},
            "not-an-object",
            {"source": "Gamma", "token": TOKEN_C},
        ])
        status = parse_status(payload)
        self.assertEqual([x.source for x in status.recent_sources],
                         ["Alpha", "Gamma"])
        with self.assertRaises(SurfError):
            parse_status([])


class SelectionContractTests(unittest.TestCase):
    def setUp(self):
        self.session = SurfSession()
        self.generation = self.session.begin("all")
        self.status = parse_status(status_payload())

    def test_previous_cycles_recent_sources_and_is_selection_only(self):
        first = self.session.choose_previous(self.generation, "all", self.status)
        self.assertEqual(first,
                         SurfSelection("Alpha", TOKEN_A))
        self.assertTrue(self.session.apply_selection(
            self.generation, "all", first))
        second = self.session.choose_previous(self.generation, "all", self.status)
        self.assertEqual(second,
                         SurfSelection("Beta", TOKEN_B))
        self.session.apply_selection(self.generation, "all", second)
        third = self.session.choose_previous(self.generation, "all", self.status)
        self.assertEqual(third,
                         SurfSelection("Gamma", TOKEN_C))
        self.session.apply_selection(self.generation, "all", third)
        wrapped = self.session.choose_previous(self.generation, "all", self.status)
        self.assertEqual(wrapped,
                         SurfSelection("Alpha", TOKEN_A))

    def test_missing_selected_token_restarts_at_first_and_empty_has_none(self):
        self.session.apply_selection(
            self.generation, "all", SurfSelection("Rotated", "d" * 32))
        chosen = self.session.choose_previous(self.generation, "all", self.status)
        self.assertEqual(chosen.source, "Alpha")
        empty = SurfStatus(source="Only", recent_sources=())
        self.assertIsNone(self.session.choose_previous(
            self.generation, "all", empty))

    def test_current_clears_selection_and_stale_generation_is_rejected(self):
        selected = self.session.choose_previous(
            self.generation, "all", self.status)
        self.session.apply_selection(self.generation, "all", selected)
        self.assertTrue(self.session.select_current(self.generation, "all"))
        self.assertIsNone(self.session.selection)
        stale = SurfSelection("Beta", TOKEN_B)
        self.session.begin("all")
        self.assertFalse(self.session.apply_selection(
            self.generation, "all", stale))
        self.assertIsNone(self.session.choose_previous(
            self.generation, "all", self.status))


class DesktopControlPathTests(unittest.TestCase):
    def test_previous_updates_selection_without_playback_command(self):
        client = FakeClient([(200, status_payload())])
        lab = Harness(client)
        result = lab.run_control("previous")
        self.assertTrue(result.ok)
        self.assertEqual(result.selection, SurfSelection("Alpha", TOKEN_A))
        self.assertEqual(lab.surf.selection, SurfSelection("Alpha", TOKEN_A))
        self.assertEqual(client.calls, [(CATALOG, "all", None, None)])
        self.assertIn("playback unchanged", result.message)
        self.assertIn(("Alpha", TOKEN_A), [(x.source, x.token)
                                          for x in [lab.surf.selection]])

    def test_favorite_previous_uses_stored_token_without_rechecking_status(self):
        client = FakeClient([(200, status_payload()), (200, {})])
        lab = Harness(client)
        lab.run_control("previous")
        client.calls.clear()
        client.responses = [(200, {})]
        result = lab.run_control("favorite_previous")
        self.assertTrue(result.ok)
        self.assertEqual(len(client.calls), 1)
        _, mode, action, payload = client.calls[0]
        self.assertEqual((mode, action), ("all", "favorite"))
        self.assertEqual(payload, {
            "source": "Alpha", "previous": True,
            "previous_token": TOKEN_A, "trace": payload["trace"],
        })
        self.assertTrue(payload["trace"].startswith("mac_"))

    def test_favorite_previous_requires_previous_selection(self):
        client = FakeClient([])
        lab = Harness(client)
        lab.surf_control("favorite_previous")
        self.assertEqual(lab.outbox, [])
        self.assertFalse(lab.ui.feedback[-1][1])
        self.assertIn("Select Previous Channel first", lab.ui.feedback[-1][0])
        self.assertEqual(client.calls, [])

    def test_current_clears_selection_without_network_or_playback(self):
        client = FakeClient([(200, status_payload())])
        lab = Harness(client)
        lab.run_control("previous")
        client.calls.clear()
        lab.surf_control("current")
        self.assertIsNone(lab.surf.selection)
        self.assertEqual(client.calls, [])
        self.assertEqual(lab.outbox[-1][1][1].action, "previous")

    def test_pause_and_resume_are_catalog_posts_not_player_pause(self):
        for action, message in (("pause", "Looping paused"),
                                ("resume", "Automatic looping resumed")):
            client = FakeClient([(200, {})])
            lab = Harness(client)
            result = lab.run_control(action)
            self.assertTrue(result.ok)
            self.assertIn(message, result.message)
            self.assertEqual(client.calls, [(CATALOG, "all", action, {})])

    def test_favorite_current_requires_server_confirmation(self):
        confirmed = Harness(FakeClient([
            (200, status_payload(source="Now Playing")),
            (204, {}),
        ]))
        result = confirmed.run_control("favorite")
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Current server source favorited")
        self.assertEqual(confirmed.client.calls[1][2], "favorite")
        self.assertEqual(confirmed.client.calls[1][3]["source"], "Now Playing")

        failed = Harness(FakeClient([
            (200, status_payload(source="Now Playing")),
            (500, {}),
        ]))
        result = failed.run_control("favorite")
        self.assertFalse(result.ok)
        self.assertIn("HTTP 500", result.message)

    def test_source_status_shows_published_source(self):
        lab = Harness(FakeClient([
            (200, status_payload(source="Published Channel", state="playing")),
        ]))
        result = lab.run_control("status")
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Surf sending: Published Channel")
        self.assertEqual(result.status.state, "playing")

    def test_controls_require_an_active_playback_session(self):
        lab = Harness(FakeClient([]))
        lab.running = False
        lab.session = None
        lab.surf_control("pause")
        self.assertEqual(lab.outbox, [])
        self.assertFalse(lab.ui.feedback[-1][1])
        self.assertIn("active Surf playback", lab.ui.feedback[-1][0])

    def test_invalid_catalog_and_auth_never_start_a_request(self):
        lab = Harness(FakeClient([]))
        lab.catalog.set("http://user:secret@catalog.local")
        lab.surf_control("pause")
        self.assertEqual(lab.outbox, [])
        self.assertFalse(lab.ui.feedback[-1][1])
        self.assertIn("credentials", lab.ui.feedback[-1][0])
        self.assertEqual(lab.client.calls, [])

        lab = Harness(FakeClient([]))
        lab.client.token = ""
        lab.surf_control("pause")
        self.assertEqual(lab.outbox, [])
        self.assertFalse(lab.ui.feedback[-1][1])
        self.assertIn("authentication unavailable", lab.ui.feedback[-1][0])
        self.assertEqual(lab.client.calls, [])

    def test_stale_generation_cannot_publish_selection(self):
        client = FakeClient([(200, status_payload())])
        lab = Harness(client)
        old_generation = lab.surf.begin("all")
        lab.surf_control("previous")
        result = lab.outbox[-1][1][1]
        self.assertEqual(result.generation, old_generation)
        lab.surf.begin("all")
        self.assertFalse(lab.apply_surf_result(result))
        self.assertIsNone(lab.surf.selection)
        self.assertIn(("surf_result_stale", {"action": "previous"}), lab.events)


if __name__ == "__main__":
    unittest.main()

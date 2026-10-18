import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pymongo.errors import DuplicateKeyError

import app

OWNER = "owner@example.com"
CO_OWNER = "co@example.com"
ADMIN = "admin@example.com"
STRANGER = "stranger@example.com"


class _Rooms:
    """In-memory rooms collection covering the queries /panel/{sid} issues."""

    def __init__(self, *rooms: dict) -> None:
        self.docs = {r["sid"]: dict(r) for r in rooms}
        # Called before each find_one; lets a test change the room "concurrently".
        self.before_find = None
        self.before_update = None

    @staticmethod
    def _matches(doc: dict, query: dict) -> bool:
        for key, cond in query.items():
            if isinstance(cond, dict) and "$in" in cond:
                if doc.get(key) not in cond["$in"]:
                    return False
            elif doc.get(key) != cond:
                return False
        return True

    async def find_one(self, query: dict):
        if self.before_find:
            hook, self.before_find = self.before_find, None
            hook(self)
        doc = self.docs.get(query["sid"])
        return copy.deepcopy(doc) if doc else None

    async def insert_one(self, doc: dict):
        if doc["sid"] in self.docs:
            raise DuplicateKeyError("E11000 duplicate key")
        self.docs[doc["sid"]] = dict(doc)

    async def update_one(self, query: dict, update: dict):
        if self.before_update:
            hook, self.before_update = self.before_update, None
            hook(self)
        doc = self.docs.get(query["sid"])
        if doc is None or not self._matches(doc, query):
            return SimpleNamespace(matched_count=0)
        doc.update(update["$set"])
        return SimpleNamespace(matched_count=1)


def _request(email: str | None = None, query: str = "") -> SimpleNamespace:
    session = {"email": email, "user_uid": f"uid-{email}"} if email else {}
    return SimpleNamespace(session=session, headers={}, url=SimpleNamespace(query=query), query_params={})


def _owned_room(**extra) -> dict:
    return {"sid": "room1", "secret_key": None, "admin_uid": "uid-owner", "admin_email": OWNER,
            "admin_last_heartbeat": None, "co_owner_emails": [CO_OWNER], **extra}


class PanelAccessTest(unittest.IsolatedAsyncioTestCase):
    async def _open(self, rooms: _Rooms, request: SimpleNamespace, sid: str = "room1"):
        users = AsyncMock()
        users.find_one.side_effect = lambda q: {"email": q["email"]} if "email" in q else None
        render = MagicMock(side_effect=lambda name, ctx: SimpleNamespace(template=name, context=ctx, headers={}))
        with (
            patch.object(app, "rooms_collection", rooms),
            patch.object(app, "users_collection", users),
            patch.object(app, "_touch_last_active", AsyncMock()),
            patch.object(app, "_invalidate_owner_override_cache", AsyncMock()),
            patch.object(app, "is_realtime_authorized", AsyncMock(return_value=True)),
            patch.object(app.templates, "TemplateResponse", render),
            patch.dict(app.EMAIL_SETTINGS, {"ADMIN_EMAILS": [ADMIN]}),
        ):
            return await app.panel(request, sid)

    def assertPanel(self, response, request) -> None:
        self.assertEqual(getattr(response, "template", None), "panel.html")
        self.assertEqual(response.context["user_secret_key"], request.session["secret_key"])

    def assertRedirect(self, response, location: str) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], location)

    async def test_owner_co_owner_and_admin_get_the_panel(self) -> None:
        for email in (OWNER, CO_OWNER, ADMIN):
            with self.subTest(email=email):
                rooms = _Rooms(_owned_room())
                request = _request(email)
                self.assertPanel(await self._open(rooms, request), request)
                # Opening the panel never moves primary ownership.
                self.assertEqual(rooms.docs["room1"]["admin_email"], OWNER)

    async def test_signed_in_non_owner_is_sent_to_the_viewer_with_its_query(self) -> None:
        rooms = _Rooms(_owned_room())
        request = _request(STRANGER, query="show=en&theme=dark")
        response = await self._open(rooms, request)
        self.assertRedirect(response, "/rt/room1?show=en&theme=dark")
        self.assertEqual(rooms.docs["room1"]["admin_email"], OWNER)
        self.assertIsNone(rooms.docs["room1"]["secret_key"])
        # Already signed in: no "sign in to manage" offer on the viewer.
        self.assertNotIn("panel_login_sid", request.session)

    async def test_anonymous_visitor_of_existing_room_gets_viewer_with_sign_in_offer_once(self) -> None:
        rooms = _Rooms(_owned_room())
        request = _request()
        self.assertRedirect(await self._open(rooms, request), "/rt/room1")
        self.assertEqual(request.session["panel_login_sid"], "room1")

        render = MagicMock(side_effect=lambda name, ctx: SimpleNamespace(template=name, context=ctx))
        with (
            patch.object(app, "get_cached_transcription", AsyncMock(return_value=None)),
            patch("app.translation_service.get_language_maps", AsyncMock(return_value={})),
            patch.object(app.templates, "TemplateResponse", render),
        ):
            first = await app.rt(request, "room1")
            second = await app.rt(request, "room1")
        self.assertTrue(first.context["owner_login"])
        self.assertFalse(second.context["owner_login"])

    async def test_anonymous_visitor_of_unowned_room_cannot_claim_it(self) -> None:
        rooms = _Rooms(_owned_room(admin_uid=None, admin_email=None))
        self.assertRedirect(await self._open(rooms, _request()), "/rt/room1")
        self.assertIsNone(rooms.docs["room1"]["admin_email"])

    async def test_signed_in_user_claims_an_unowned_room(self) -> None:
        rooms = _Rooms(_owned_room(admin_uid=None, admin_email=None))
        request = _request(STRANGER)
        self.assertPanel(await self._open(rooms, request), request)
        self.assertEqual(rooms.docs["room1"]["admin_email"], STRANGER)

    async def test_missing_room_sends_anonymous_visitor_to_login_and_creates_nothing(self) -> None:
        rooms = _Rooms()
        self.assertRedirect(await self._open(rooms, _request()), "/login?next=/panel/room1")
        self.assertEqual(rooms.docs, {})

    async def test_missing_room_is_created_owned_by_the_signed_in_visitor(self) -> None:
        rooms = _Rooms()
        request = _request(STRANGER)
        self.assertPanel(await self._open(rooms, request), request)
        self.assertEqual(rooms.docs["room1"]["admin_email"], STRANGER)
        self.assertEqual(rooms.docs["room1"]["secret_key"], request.session["secret_key"])

    async def test_creation_race_is_settled_by_ownership(self) -> None:
        # Both saw no room; the other user's insert landed first.
        rooms = _Rooms()
        original_insert = rooms.insert_one

        async def insert_after_rival(doc):
            rooms.docs["room1"] = _owned_room()
            await original_insert(doc)

        rooms.insert_one = insert_after_rival
        request = _request(STRANGER)
        self.assertRedirect(await self._open(rooms, request), "/rt/room1")
        self.assertEqual(rooms.docs["room1"]["admin_email"], OWNER)
        self.assertNotIn("secret_key", request.session)

    async def test_concurrent_claim_of_unowned_room_keeps_the_first_owner(self) -> None:
        rooms = _Rooms(_owned_room(admin_uid=None, admin_email=None))
        # Another user claims it between this request's read and its write.
        rooms.before_update = lambda r: r.docs["room1"].update(admin_email=OWNER)
        request = _request(STRANGER)
        self.assertRedirect(await self._open(rooms, request), "/rt/room1")
        self.assertEqual(rooms.docs["room1"]["admin_email"], OWNER)

    async def test_co_owner_reclaiming_legacy_room_keeps_its_admin_uid_owner(self) -> None:
        # Legacy rooms name the owner only through admin_uid, which the lock claim
        # hands to the caller; the resolved owner must be persisted first.
        rooms = _Rooms(_owned_room(admin_email=None))
        request = _request(CO_OWNER)
        users = AsyncMock()
        users.find_one.side_effect = lambda q: {"email": OWNER} if q.get("user_uid") == "uid-owner" else {"email": q.get("email")}
        render = MagicMock(side_effect=lambda name, ctx: SimpleNamespace(template=name, context=ctx, headers={}))
        with (
            patch.object(app, "rooms_collection", rooms),
            patch.object(app, "users_collection", users),
            patch.object(app, "_touch_last_active", AsyncMock()),
            patch.object(app, "_invalidate_owner_override_cache", AsyncMock()),
            patch.object(app, "is_realtime_authorized", AsyncMock(return_value=True)),
            patch.object(app.templates, "TemplateResponse", render),
        ):
            response = await app.panel(request, "room1")
        self.assertPanel(response, request)
        self.assertEqual(rooms.docs["room1"]["admin_email"], OWNER)
        self.assertEqual(rooms.docs["room1"]["admin_uid"], f"uid-{CO_OWNER}")


class LoginNextTest(unittest.IsolatedAsyncioTestCase):
    def test_only_same_origin_paths_are_accepted(self) -> None:
        self.assertEqual(app._safe_next_path("/panel/room1"), "/panel/room1")
        for unsafe in (None, "", "panel/room1", "https://evil.example", "//evil.example",
                       "/\\evil.example", "/\t/evil.example"):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(app._safe_next_path(unsafe))

    async def test_signed_in_visit_to_login_follows_next(self) -> None:
        request = SimpleNamespace(session={"email": STRANGER}, query_params={"next": "/panel/room1"})
        response = await app.login_page(request)
        self.assertEqual(response.headers["location"], "/panel/room1")


if __name__ == "__main__":
    unittest.main()

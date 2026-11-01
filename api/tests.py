"""Tests für den Delta-Abruf auf GET /api/v4/organizations/content/."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils.timezone import localtime

from content.models import (
    Article,
    Category,
    DeletedContent,
    EventV4,
    Event_Occurrence,
    Organization,
    Source,
)


class OrganizationContentDeltaTests(TestCase):
    """changed_since, removed-Listen und die Sprachpaar-Logik im Delta."""

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            name="news", title="News", description="", rank=1
        )
        cls.organization = Organization.objects.create(name="Testorga", title="Testorga")
        cls.other_organization = Organization.objects.create(
            name="Fremdorga", title="Fremdorga"
        )
        cls.source = Source.objects.create(
            name="Testquelle",
            type="local",
            organization=cls.organization,
            default_category=cls.category,
        )

    def setUp(self):
        self.url = reverse("v4:organization-content")
        self.headers = {"HTTP_X_DEVICE_ID": "test-device"}

    def get(self, **params):
        params.setdefault("organizations", str(self.organization.id))
        return self.client.get(self.url, params, **self.headers)

    # Anlegen und Ändern passieren im Test innerhalb derselben Millisekunde. Statt
    # zu schlafen werden die Fixtures nach dem Anlegen eine Stunde zurückdatiert;
    # changed_since liegt dann dazwischen und trennt "angelegt" von "geändert"
    # zuverlässig. update() statt save(), sonst würde auto_now sofort überschreiben.
    def backdate(self, *objects):
        stale = localtime() - timedelta(hours=1)
        for obj in objects:
            type(obj).objects.filter(pk=obj.pk).update(updated_at=stale)

    @property
    def cutoff(self):
        """Zeitpunkt zwischen dem Zurückdatieren und jeder späteren Änderung."""
        return (localtime() - timedelta(minutes=30)).isoformat()

    def make_article(self, title="Artikel", language="de", published=True):
        return Article.objects.create(
            title=title, source=self.source, published=published, language=language
        )

    def make_event(self, title="Event", language="de", published=True):
        start = localtime() + timedelta(days=3)
        event = EventV4.objects.create(
            title=title,
            source=self.source,
            published=published,
            language=language,
            start_date=start,
        )
        Event_Occurrence.objects.create(
            event=event, start_datetime=start, end_datetime=start + timedelta(hours=2)
        )
        return event

    # --- Grundverhalten ---------------------------------------------------

    def test_response_always_contains_server_time(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertIn("server_time", response.data)

    def test_invalid_changed_since_returns_400(self):
        response = self.get(changed_since="letzte Woche")
        self.assertEqual(response.status_code, 400)

    def test_changed_since_accepts_plain_date(self):
        self.make_article()
        response = self.get(changed_since=str(localtime().date() - timedelta(days=1)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["articles"]["count"], 1)

    # --- Delta-Filter -----------------------------------------------------

    def test_unchanged_content_is_excluded(self):
        self.backdate(self.make_article(), self.make_event())

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(response.data["articles"]["count"], 0)
        self.assertEqual(response.data["events"]["count"], 0)

    def test_changed_article_is_included(self):
        article = self.make_article()
        self.backdate(article)

        article.title = "Geändert"
        article.save()

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(response.data["articles"]["count"], 1)

    def test_change_to_english_version_surfaces_the_pair(self):
        """Anker ist die deutsche Version - eine Änderung nur an der englischen
        Fassung muss das Paar trotzdem ins Delta bringen."""
        german = self.make_article(title="Deutsch")
        english = self.make_article(title="English", language="en")
        german.translations.add(english)
        self.backdate(german, english)

        english.title = "English changed"
        english.save()

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(response.data["articles"]["count"], 1)
        pair = response.data["articles"]["results"][0]
        self.assertEqual(pair["de"]["title"], "Deutsch")
        self.assertEqual(pair["en"]["title"], "English changed")

    def test_occurrence_change_marks_event_as_changed(self):
        event = self.make_event()
        self.backdate(event)

        occurrence = event.occurrences.first()
        occurrence.start_datetime = occurrence.start_datetime + timedelta(hours=1)
        occurrence.end_datetime = occurrence.end_datetime + timedelta(hours=1)
        occurrence.save()

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(response.data["events"]["count"], 1)

    def test_occurrence_deletion_marks_event_as_changed(self):
        event = self.make_event()
        start = localtime() + timedelta(days=4)
        extra = Event_Occurrence.objects.create(
            event=event, start_datetime=start, end_datetime=start + timedelta(hours=1)
        )
        self.backdate(event)

        extra.delete()

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(response.data["events"]["count"], 1)

    # --- removed ----------------------------------------------------------

    def test_removed_absent_without_changed_since(self):
        response = self.get()
        self.assertNotIn("removed", response.data["articles"])

    def test_deleted_article_appears_as_removed(self):
        article = self.make_article()
        article_id = article.id
        self.backdate(article)

        article.delete()

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(
            response.data["articles"]["removed"],
            [
                {
                    "type": "article",
                    "id": article_id,
                    "reason": "deleted",
                    "title": article.title,
                }
            ],
        )

    def test_deleted_event_appears_as_removed(self):
        event = self.make_event()
        event_id = event.id
        self.backdate(event)

        event.delete()

        self.assertEqual(
            self.get(changed_since=self.cutoff).data["events"]["removed"],
            [
                {
                    "type": "event",
                    "id": event_id,
                    "reason": "deleted",
                    "title": event.title,
                }
            ],
        )

    def test_unpublished_article_appears_as_removed(self):
        article = self.make_article()
        self.backdate(article)

        article.published = False
        article.save()

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(response.data["articles"]["count"], 0)
        self.assertEqual(
            response.data["articles"]["removed"],
            [
                {
                    "type": "article",
                    "id": article.id,
                    "reason": "unpublished",
                    "title": article.title,
                }
            ],
        )

    def test_never_published_content_is_not_reported_as_removed(self):
        """Ein nie veröffentlichter Import darf nicht als "entfernt" gemeldet werden -
        der Client hatte ihn nie."""
        article = self.make_article(published=False)
        self.backdate(article)

        article.title = "Von einem Batch-Lauf angefasst"
        article.save()

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(response.data["articles"]["removed"], [])

    def test_removed_is_scoped_to_requested_organizations(self):
        other_source = Source.objects.create(
            name="Fremdquelle",
            type="local",
            organization=self.other_organization,
            default_category=self.category,
        )
        foreign = Article.objects.create(
            title="Fremd", source=other_source, published=True
        )
        self.backdate(foreign)

        foreign.delete()

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(response.data["articles"]["removed"], [])

    def test_tombstone_records_organization(self):
        article = self.make_article()
        article.delete()

        tombstone = DeletedContent.objects.get(content_type="article")
        self.assertEqual(tombstone.organization_id, self.organization.id)

    def test_tombstone_records_title(self):
        """Der Titel muss beim Loeschen mitgeschrieben werden - danach steht er
        nirgends mehr."""
        article = self.make_article(title="Bald geloescht")
        article.delete()

        tombstone = DeletedContent.objects.get(content_type="article")
        self.assertEqual(tombstone.title, "Bald geloescht")

    def test_removed_title_empty_for_old_tombstone(self):
        """Grabsteine ohne Titel (vor Einfuehrung des Feldes) liefern einen leeren
        String, nicht null - der Client faellt dann auf die ID zurueck."""
        article = self.make_article()
        article_id = article.id
        self.backdate(article)
        article.delete()
        DeletedContent.objects.filter(object_id=article_id).update(title="")

        response = self.get(changed_since=self.cutoff)
        self.assertEqual(
            response.data["articles"]["removed"],
            [
                {
                    "type": "article",
                    "id": article_id,
                    "reason": "deleted",
                    "title": "",
                }
            ],
        )

    def test_response_has_no_full_sync_flag(self):
        """Die Antwort trägt keine Sync-Hinweise mehr - ein sehr altes
        changed_since liefert einfach das, was noch bekannt ist."""
        stale = localtime() - timedelta(days=365)
        response = self.get(changed_since=stale.isoformat())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("full_sync_required", response.data)

    # --- ordering ---------------------------------------------------------

    def test_ordering_changed_puts_latest_change_first(self):
        first = self.make_article(title="Zuerst")
        second = self.make_article(title="Danach")
        first.title = "Zuerst, aber zuletzt geändert"
        first.save()

        response = self.get(ordering="changed")
        titles = [pair["de"]["title"] for pair in response.data["articles"]["results"]]
        self.assertEqual(titles[0], "Zuerst, aber zuletzt geändert")
        self.assertEqual(titles[1], second.title)

    def test_invalid_ordering_returns_400(self):
        self.assertEqual(self.get(ordering="bogus").status_code, 400)


class AreaWelcomeArticleTests(TestCase):
    def _create_area(self):
        return self.client.post(
            "/api/v4/area/",
            {"area": "Neustadt", "zip_code": "12345", "latitude": 53.1, "longitude": 8.8},
            content_type="application/json",
        )

    def test_without_source_creates_area_without_article(self):
        response = self._create_area()
        self.assertEqual(response.status_code, 201)
        self.assertFalse(Article.objects.filter(title="Willkommen bei molo!").exists())

    def test_uses_configured_source_and_tag(self):
        from content.models import Tag

        category = Category.objects.create(name="news", title="News", description="", rank=1)
        organization = Organization.objects.create(name="Redaktion", title="Redaktion")
        source = Source.objects.create(
            name="Redaktion", type="local", organization=organization, default_category=category,
        )
        tag = Tag.objects.create(name="Willkommen", category=category)
        with self.settings(AREA_WELCOME_SOURCE_ID=source.pk, AREA_WELCOME_TAG_ID=tag.pk):
            self.assertEqual(self._create_area().status_code, 201)
        article = Article.objects.get(title="Willkommen bei molo!")
        self.assertEqual(article.source, source)
        self.assertEqual(list(article.tags.all()), [tag])


class FreshInstanceTests(TestCase):
    """Eine leere Instanz (nur Migrationen) muss für die App funktionieren."""

    def test_default_categories_exist(self):
        self.assertEqual(set(Category.objects.values_list("pk", flat=True)), {1, 2})

    def test_new_device_profile_without_areas(self):
        from content.models import AppUser

        AppUser.objects.create(device_id="neu-1")
        for path in ("/api/v4/users/summary/", "/api/v4/articles/", "/api/v4/events/"):
            response = self.client.get(path, HTTP_X_DEVICE_ID="neu-2")
            self.assertLess(response.status_code, 500, path)
        self.assertTrue(AppUser.objects.filter(device_id="neu-2").exists())


class ContentCreateValidationTests(TestCase):
    """Fehlende Pflichtangaben ergeben 400, ohne halbe Artikel oder Events zu speichern."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group

        from content.models import Area, Tag, User

        cls.category = Category.objects.get(pk=1)
        cls.tag = Tag.objects.create(name="Thema", category=cls.category)
        cls.area = Area.objects.create(name="Stadt", zip="12345", latitude=53.0, longitude=8.8)
        cls.user = User.objects.create_user(username="autor", email="a@example.org", password="x", is_active=True)
        cls.user.groups.add(Group.objects.get(name="Contributors"))
        organization = Organization.objects.create(name="Orga", title="Orga")
        cls.source = Source.objects.create(
            name="Quelle", type="local", organization=organization,
            default_category=cls.category, related_user=cls.user,
        )

    def _post(self, path, data):
        from rest_framework_simplejwt.tokens import AccessToken

        return self.client.post(path, data, content_type="application/json",
                                HTTP_AUTHORIZATION=str(AccessToken.for_user(self.user)))

    def test_article_without_tags_is_rejected_before_saving(self):
        response = self._post("/api/v4/articles/", {"title": "Ohne Themen", "content": "c", "area": [self.area.pk]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Article.objects.filter(title="Ohne Themen").exists())

    def test_article_with_unknown_tag_is_rejected(self):
        response = self._post("/api/v4/articles/", {
            "title": "Falsches Thema", "content": "c", "area": [self.area.pk], "article_tags": [999999],
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Article.objects.filter(title="Falsches Thema").exists())

    def test_article_with_tags_is_created(self):
        response = self._post("/api/v4/articles/", {
            "title": "Mit Themen", "content": "c", "area": [self.area.pk], "article_tags": [self.tag.pk],
        })
        self.assertEqual(response.status_code, 201)
        article = Article.objects.get(title="Mit Themen")
        self.assertEqual(list(article.tags.all()), [self.tag])

    def test_event_without_tags_is_rejected_before_saving(self):
        from content.models import EventV4

        response = self._post("/api/v4/events/", {
            "title": "Ohne Themen", "content": "c", "street": "", "town": "Stadt",
            "occurrences": [{"event_start_date": "2030-01-01T10:00:00Z", "event_end_date": "2030-01-01T12:00:00Z"}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(EventV4.objects.filter(title="Ohne Themen").exists())


class SqliteSettingsTests(TestCase):
    def test_postgres_db_name_is_not_used_as_sqlite_file(self):
        import importlib
        import os
        from unittest import mock

        import molo.settings.base as base

        with mock.patch.dict(os.environ, {"DB_ENGINE": "django.db.backends.sqlite3", "DB_NAME": "molo", "SQLITE_PATH": ""}):
            reloaded = importlib.reload(base)
            self.assertTrue(reloaded.DATABASES["default"]["NAME"].endswith("db.sqlite3"))
        importlib.reload(base)


class AccountEndpointTests(TestCase):
    def test_registration_with_invalid_area_creates_no_account(self):
        from content.models import User

        response = self.client.post("/api/v4/users/", {
            "username": "neu", "email": "neu@example.org", "password": "Ein-gutes-Passwort-42",
            "display_name": "Neue Orga", "area": 999999,
        }, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="neu").exists())

    def test_password_reset_requires_email(self):
        from content.models import User

        User.objects.create_user(username="a", password="x")
        User.objects.create_user(username="b", password="x")
        response = self.client.post("/api/v4/users/request-password-reset/", {}, content_type="application/json")
        self.assertEqual(response.status_code, 400)


class AccountWithoutSourceTests(TestCase):
    """Ein Redaktionskonto ohne eigene Quelle bekommt 400/401, keinen Serverfehler."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group

        from content.models import Area, EventV4, Tag, User

        category = Category.objects.get(pk=1)
        cls.area = Area.objects.create(name="Stadt", zip="12345", latitude=53.0, longitude=8.8)
        cls.tag = Tag.objects.create(name="Thema", category=category)
        cls.event_tag = Tag.objects.create(name="Konzert", category=Category.objects.get(pk=2), type="event")
        owner = User.objects.create_user(username="besitzer", email="b@example.org", password="x")
        organization = Organization.objects.create(name="Orga", title="Orga")
        source = Source.objects.create(
            name="Quelle", type="local", organization=organization, default_category=category, related_user=owner,
        )
        cls.article = Article.objects.create(title="Fremd", source=source, published=True)
        cls.event = EventV4.objects.create(title="Fremdes Event", source=source, published=True)
        cls.editor = User.objects.create_user(username="editor", email="e@example.org", password="x")
        cls.editor.groups.add(Group.objects.get(name="Editors"))

    def _request(self, method, path, data=None):
        from rest_framework_simplejwt.tokens import AccessToken

        return getattr(self.client, method)(
            path, data, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(self.editor)}",
        )

    def test_create_without_source_is_rejected(self):
        response = self._request("post", "/api/v4/articles/", {
            "title": "Neu", "content": "c", "area": [self.area.pk], "article_tags": [self.tag.pk],
        })
        self.assertEqual(response.status_code, 400)
        response = self._request("post", "/api/v4/events/", {
            "title": "Neu", "content": "c", "area": [self.area.pk], "event_tags": [self.event_tag.pk],
            "occurrences": [{"event_start_date": "2030-01-01T19:00:00+01:00", "event_end_date": "2030-01-01T21:00:00+01:00"}],
        })
        self.assertEqual(response.status_code, 400)

    def test_foreign_content_cannot_be_changed_or_deleted(self):
        for method, path in (
            ("put", f"/api/v4/articles/{self.article.pk}/"),
            ("delete", f"/api/v4/articles/{self.article.pk}/"),
            ("put", f"/api/v4/events/{self.event.pk}/"),
            ("delete", f"/api/v4/events/{self.event.pk}/"),
        ):
            response = self._request(method, path, {"title": "Geändert"})
            self.assertIn(response.status_code, (401, 403), f"{method} {path}: {response.status_code}")
        self.assertTrue(Article.objects.filter(pk=self.article.pk, title="Fremd").exists())
        from content.models import EventV4

        self.assertTrue(EventV4.objects.filter(pk=self.event.pk, title="Fremdes Event").exists())


class LinkFieldTests(TestCase):
    def test_free_text_and_other_schemes_are_kept(self):
        from content.utils.urls import is_safe_link

        for value in ("Tickets: Abendkasse", "webcal://example.org/cal.ics", "geo:53.07,8.80", "https://example.org"):
            self.assertTrue(is_safe_link(value), value)

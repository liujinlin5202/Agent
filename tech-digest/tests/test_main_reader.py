"""Daily integration uses real review contracts and only offline mocks/temp databases."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import main
from app import publication_gate, report
from app.store import Store
from scripts.content_report import _read_report_data
from tests.reader_fixtures import article_packet, passing_review
from tests.test_main import FakeStore

DAY = "2026-10-09"
BODY = "## 为什么缓存能减少重复读取？\n\n固定输入的缓存对照。\n".strip()
UNSET = object()


def ready_result():
    source, draft = article_packet()
    item = {"type": "news", "source": "hacker-news", "title": "缓存实验",
            "url": source["url"], "summary": "固定输入的缓存对照。"}
    return {"status": "ready", "title": draft["title"], "markdown": BODY,
            "draft": draft, "source_packet": source,
            "review": passing_review(draft["title"], BODY, source), "item": item,
            "attempts": [], "reasons": []}


class TrackedStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.cleanup_count = 0
        self.issue_reads = 0
        self.closed = False

    def next_issue_no(self):
        self.issue_reads += 1
        return 1

    def get_issue_no(self, day):
        self.issue_reads += 1
        return None

    def cleanup(self):
        self.cleanup_count += 1

    def close(self):
        self.closed = True


class TestDailyReaderIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "output"
        self.data = Path(self.temp.name) / "data"
        self.store = TrackedStore()
        self.valid = ready_result()
        self.pref = main.preference.Preference(mode="default", text="")

    def run_once(self, result=UNSET, *, material="pool", force=False, dry=False,
                 pipeline_error=None, published=(123, "/postdetail/123", None),
                 existing_store=None, fetch_error=None):
        result = self.valid if result is UNSET else result
        store = existing_store or self.store
        news = [deepcopy(self.valid["item"])]
        stats = {"hacker-news": {"fetched": 1, "error": None}}
        with ExitStack() as stack:
            stack.enter_context(patch("main.Store", return_value=store))
            stack.enter_context(patch("main.settings.output_dir", self.output))
            stack.enter_context(patch("main.settings.data_dir", Path(self.temp.name) / "data"))
            stack.enter_context(patch("main.settings.daily_publish", True))
            stack.enter_context(patch("main.settings.publish_weekend", True))
            stack.enter_context(patch("main.settings.market_refresh_token", "offline-placeholder"))
            stack.enter_context(patch("main.settings.market_user_telephone", "offline-user"))
            stack.enter_context(patch("main.settings.sse_market_api_key", "offline-model"))
            stack.enter_context(patch("main.settings.deepseek_api_key", ""))
            stack.enter_context(patch("main.should_skip", return_value=(False, "")))
            clock = stack.enter_context(patch("main.date"))
            clock.today.return_value = date.fromisoformat(DAY)
            clock.fromisoformat.side_effect = date.fromisoformat
            pool = stack.enter_context(patch("main._pool_daily_materials",
                                            return_value=(news, []) if material == "pool" else None))
            fetch = stack.enter_context(patch("main.fetch_all", return_value=(news, stats),
                                             side_effect=fetch_error))
            stack.enter_context(patch("main.preference.load_preference", return_value=self.pref))
            stack.enter_context(patch("main._dedup_history", return_value=[]))
            pipeline = stack.enter_context(patch("main.editorial.run_pipeline", return_value=result,
                                                side_effect=pipeline_error))
            publish = stack.enter_context(patch("app.publisher.publish_md", return_value=published))
            token = stack.enter_context(patch("app.publisher.refresh_access_token", return_value={"ok": 1}))
            stack.enter_context(patch("main._verify_published"))
            consume = stack.enter_context(patch("main._mark_pool_consumed"))
            sample = stack.enter_context(patch("main._sample_views"))
            recent = stack.enter_context(patch.object(store, "recent_daily_titles", return_value=["近期标题"]))
            forbidden = {name: stack.enter_context(patch("main." + name)) for name in
                         ("_pick_article", "_fetch_body", "_pick_post_title", "_prefetch_gate", "render_repost")}
            # The real pure gate validates all final strings; do not replace it with a success stub.
            gate = stack.enter_context(patch.object(publication_gate, "decide_publication",
                                                   wraps=publication_gate.decide_publication))
            rc = main.run_daily(force=force, dry_run=dry)
        return rc, {"pipeline": pipeline, "publish": publish, "consume": consume,
                    "sample": sample, "recent": recent, "forbidden": forbidden,
                    "pool": pool, "fetch": fetch, "gate": gate, "token": token}

    def assert_blocked(self, result, expected_rc, *, material="pool"):
        formal = self.output / f"{DAY}-daily.md"
        self.output.mkdir(exist_ok=True)
        formal.write_text("unchanged formal output", encoding="utf-8")
        prior_drafts = set((self.data / "drafts").glob("*"))
        rc, calls = self.run_once(result, material=material)
        self.assertEqual(rc, expected_rc)
        calls["publish"].assert_not_called()
        calls["consume"].assert_not_called()
        calls["sample"].assert_not_called()
        self.assertEqual(self.store.saved, [])
        self.assertEqual(self.store.posted, {})
        self.assertEqual(self.store.cleanup_count, 0)
        self.assertEqual(self.store.issue_reads, 0)
        self.assertEqual(formal.read_text(encoding="utf-8"), "unchanged formal output")
        for mock in calls["forbidden"].values():
            mock.assert_not_called()
        self.assertTrue(self.store.closed)
        # 缺口②（2026-10-13 评审）：拒稿必须落在 state 盘（data_dir），output/
        # 在秋坞随容器蒸发——失败草稿是「今天为什么没发」的唯一归因证据。
        drafts = list(set((self.data / "drafts").glob("*")) - prior_drafts)
        self.assertTrue(drafts)
        record = json.loads(next(p for p in drafts if p.suffix == ".json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "generation_error" if expected_rc else "skip_quality")
        return calls

    def test_low_score_claimed_ready_is_blocked_for_pool_and_live(self):
        for material in ("pool", "live"):
            with self.subTest(material=material):
                self.store = TrackedStore()
                result = ready_result()
                result["review"]["scores"]["depth"] = 6
                calls = self.assert_blocked(result, 0, material=material)
                self.assertEqual(calls["gate"].call_count, 1)

    def test_stale_title_or_body_is_blocked(self):
        for material in ("pool", "live"):
            for field in ("title", "markdown"):
                with self.subTest(material=material, field=field):
                    self.store = TrackedStore()
                    result = ready_result()
                    result[field] += "审核后变化"
                    self.assert_blocked(result, 1, material=material)

    def test_changed_source_metadata_invalidates_old_review_for_both_sources(self):
        for material in ("pool", "live"):
            for field, value in (("author", "changed author"), ("title", "changed source title"),
                                 ("published_at", "2026-10-01T00:00:00+00:00"),
                                 ("publication_status", "preprint")):
                with self.subTest(material=material, field=field):
                    self.store = TrackedStore()
                    result = ready_result()
                    result["source_packet"][field] = value
                    self.assert_blocked(result, 1, material=material)

    def test_missing_review_and_review_error_are_generation_errors(self):
        for material in ("pool", "live"):
            for review in (None, {"status": "error"}):
                with self.subTest(material=material, review=review):
                    self.store = TrackedStore()
                    result = ready_result()
                    result["review"] = review
                    self.assert_blocked(result, 1, material=material)

    def test_malformed_ready_payloads_do_not_reach_effects(self):
        for material in ("pool", "live"):
            for field, value in (("draft", None), ("source_packet", []), ("markdown", ""),
                                 ("item", []), ("status", "unknown")):
                with self.subTest(material=material, field=field):
                    self.store = TrackedStore()
                    result = ready_result()
                    result[field] = value
                    self.assert_blocked(result, 1, material=material)

    def test_non_mapping_results_are_generation_errors(self):
        for result in (None, [], "invalid"):
            with self.subTest(result=result):
                self.store = TrackedStore()
                self.assert_blocked(result, 1)

    def test_producer_skip_and_failure_cannot_be_overridden_by_passing_gate(self):
        for status, expected in (("skip_quality", 0), ("generation_error", 1)):
            with self.subTest(status=status):
                self.store = TrackedStore()
                result = ready_result()
                result.update(status=status, reasons=["producer_declined"])
                calls = self.assert_blocked(result, expected)
                self.assertEqual(calls["gate"].call_count, 1)

    def test_pipeline_exception_has_no_legacy_fallback(self):
        for material in ("pool", "live"):
            with self.subTest(material=material):
                self.store = TrackedStore()
                rc, calls = self.run_once(material=material, pipeline_error=RuntimeError("offline model failed"))
                self.assertEqual(rc, 1)
                calls["publish"].assert_not_called()
                calls["consume"].assert_not_called()
                self.assertEqual(self.store.saved, [])
                self.assertEqual(self.store.cleanup_count, 0)
                for mock in calls["forbidden"].values():
                    mock.assert_not_called()
                self.assertTrue(any(entry[2].get("detail") == "generation_error" for entry in self.store.logs))

    def test_incomplete_source_claimed_ready_is_quality_skipped_for_both_sources(self):
        for material in ("pool", "live"):
            with self.subTest(material=material):
                self.store = TrackedStore()
                result = ready_result()
                result["source_packet"]["issues"] = ["truncated"]
                result["review"] = passing_review(result["title"], BODY, result["source_packet"])
                self.assert_blocked(result, 0, material=material)

    def test_live_collection_failure_does_not_run_pipeline_or_formal_effects(self):
        rc, calls = self.run_once(material="live", fetch_error=RuntimeError("offline collection failed"))
        self.assertEqual(rc, 1)
        for key in ("pipeline", "publish", "consume", "sample", "recent"):
            calls[key].assert_not_called()
        self.assertEqual(self.store.saved, [])
        self.assertEqual(self.store.cleanup_count, 0)
        self.assertEqual(self.store.issue_reads, 0)
        self.assertFalse((self.output / f"{DAY}-daily.md").exists())
        self.assertTrue(self.store.closed)

    def test_gate_exception_is_generation_error_before_formal_effects(self):
        with patch.object(publication_gate, "decide_publication", side_effect=RuntimeError("offline gate failed")):
            self.assert_blocked(ready_result(), 1)

    def test_both_material_sources_use_pipeline_and_publish_exact_reviewed_strings(self):
        for material in ("pool", "live"):
            with self.subTest(material=material):
                self.store = TrackedStore()
                rc, calls = self.run_once(material=material)
                self.assertEqual(rc, 0)
                self.assertEqual(calls["pipeline"].call_args.kwargs["recent_titles"], ["近期标题"])
                self.assertIs(calls["pipeline"].call_args.kwargs["preference"], self.pref)
                self.assertEqual(calls["pipeline"].call_args.args[0][0]["url"], self.valid["item"]["url"])
                calls["publish"].assert_called_once_with(self.valid["title"], BODY, private=False)
                calls["consume"].assert_called_once_with(self.valid["item"], DAY)
                self.assertEqual(calls["gate"].call_args.args,
                                 (self.valid["title"], BODY, self.valid["draft"], self.valid["source_packet"], self.valid["review"]))
                self.assertEqual((self.output / f"{DAY}-daily.md").read_bytes(), BODY.encode("utf-8"))
                self.assertEqual(self.store.saved[0][3], self.valid["title"])
                detail = next(d for _, _, d in self.store.logs if d.get("detail") == "published")
                self.assertEqual(detail["title_hash"], sha256(self.valid["title"].encode("utf-8")).hexdigest())
                self.assertEqual(detail["body_hash"], sha256(BODY.encode("utf-8")).hexdigest())
                self.assertEqual(detail["source_hash"], self.valid["source_packet"]["source_hash"])
                self.assertEqual(detail["draft_hash"], self.valid["review"]["draft_hash"])
                self.assertEqual(detail["word_count"], len(BODY))
                self.assertEqual(detail["evidence_count"], len(self.valid["draft"]["evidence_ids"]))
                for mock in calls["forbidden"].values():
                    mock.assert_not_called()

    def test_already_posted_day_exits_before_new_material_even_force(self):
        self.store.posted[DAY] = 123
        for force in (False, True):
            with self.subTest(force=force):
                rc, calls = self.run_once(force=force)
                self.assertEqual(rc, 0)
                for key in ("pool", "fetch", "pipeline", "publish", "consume", "recent"):
                    calls[key].assert_not_called()
                self.assertEqual(self.store.saved, [])

    def test_real_database_quality_skip_preserves_snapshot_issue_and_history(self):
        store = Store(Path(self.temp.name) / "fixture.db")
        original = store.save_daily_v2(DAY, [{"title": "earlier snapshot"}], "earlier.md", post_title="earlier")
        next_issue = store.next_issue_no()
        result = ready_result()
        result["review"]["scores"]["depth"] = 6
        rc, calls = self.run_once(result, existing_store=store)
        self.assertEqual(rc, 0)
        calls["publish"].assert_not_called()
        reopened = Store(Path(self.temp.name) / "fixture.db")
        try:
            self.assertEqual(reopened.get_issue_no(DAY), original)
            self.assertEqual(reopened.next_issue_no(), next_issue)
            self.assertEqual(reopened.daily_title(DAY), "earlier")
        finally:
            reopened.close()

    def test_unpublished_snapshot_retry_reuses_issue_and_then_duplicate_does_not_consume(self):
        path = Path(self.temp.name) / "fixture.db"
        store = Store(path)
        issue = store.save_daily_v2(DAY, [{"title": "earlier"}], "old.md")
        next_issue = store.next_issue_no()
        rc, calls = self.run_once(existing_store=store)
        self.assertEqual(rc, 0)
        calls["publish"].assert_called_once()
        calls["consume"].assert_called_once()
        reopened = Store(path)
        self.assertEqual(reopened.get_issue_no(DAY), issue)
        self.assertEqual(reopened.next_issue_no(), next_issue)
        rc, calls = self.run_once(existing_store=reopened, force=True)
        self.assertEqual(rc, 0)
        calls["pipeline"].assert_not_called()
        calls["consume"].assert_not_called()

    def test_publish_failure_keeps_candidate_unconsumed_and_allows_retry(self):
        rc, calls = self.run_once(published=(None, None, "offline failure"))
        self.assertEqual(calls["publish"].call_count, 2)
        calls["consume"].assert_not_called()
        self.assertEqual(self.store.posted, {})
        rc, calls = self.run_once()
        self.assertEqual(rc, 0)
        calls["publish"].assert_called_once()
        calls["consume"].assert_called_once()

    def test_dry_run_has_no_snapshot_posted_marker_or_consumption(self):
        rc, calls = self.run_once(dry=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.saved, [])
        self.assertEqual(self.store.posted, {})
        calls["publish"].assert_not_called()
        calls["consume"].assert_not_called()
        calls["token"].assert_called_once_with()


class TestPublishMetadataAndSampling(unittest.TestCase):
    def publish(self, store, **kwargs):
        with patch("main.settings.daily_publish", True), \
             patch("main.settings.publish_weekend", True), \
             patch("main.settings.market_refresh_token", "offline-placeholder"), \
             patch("main.settings.market_user_telephone", "offline-user"), \
             patch("app.publisher.publish_md", return_value=(123, "/postdetail/123", None)) as mock, \
             patch("main._verify_published"), \
             patch("app.publisher.refresh_access_token", return_value={"ok": 1}):
            success = main._publish_md(store, "daily", DAY, "原样标题", BODY,
                                       content_meta={"article_type": "mechanism", "learning_question": "为何缓存有效？",
                                                     "published_at": "1999-01-01T00:00:00+00:00",
                                                     "title_hash": "obsolete", "body_hash": "obsolete",
                                                     "word_count": 1, "evidence_count": 3,
                                                     "source_hash": "offline-source-hash"}, **kwargs)
        return success, mock

    def test_confirmed_publish_returns_true_with_utc_local_confirmation_metadata(self):
        store = TrackedStore()
        before = datetime.now(timezone.utc)
        success, mock = self.publish(store, dry_run=False)
        after = datetime.now(timezone.utc)
        self.assertIs(success, True)
        detail = next(d for task, status, d in store.logs if d.get("detail") == "published")
        stamp = datetime.fromisoformat(detail["published_at"])
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        self.assertLessEqual(before.replace(microsecond=0), stamp)
        self.assertLessEqual(stamp, after)
        self.assertEqual(detail["time_basis"], "local_confirmation")
        self.assertEqual(detail["content_version"], "research-v1")
        self.assertEqual(detail["article_type"], "mechanism")
        self.assertEqual(detail["learning_question"], "为何缓存有效？")
        self.assertEqual(detail["title_hash"], sha256("原样标题".encode("utf-8")).hexdigest())
        self.assertEqual(detail["body_hash"], sha256(BODY.encode("utf-8")).hexdigest())
        self.assertEqual(detail["word_count"], len(BODY))
        self.assertEqual(detail["evidence_count"], 3)
        self.assertEqual(detail["source_hash"], "offline-source-hash")
        mock.assert_called_once_with("原样标题", BODY, private=False)

    def test_duplicate_and_dry_run_return_false(self):
        for posted, dry in ((True, False), (False, True)):
            with self.subTest(posted=posted, dry=dry):
                store = TrackedStore()
                if posted:
                    store.posted[DAY] = 123
                success, mock = self.publish(store, dry_run=dry)
                self.assertIs(success, False)
                mock.assert_not_called()
                self.assertFalse(any(d.get("detail") == "published" for _, _, d in store.logs))

    def test_new_samples_are_utc_aware_old_rows_remain_naive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "fixture.db")
            try:
                store.conn.execute("INSERT INTO post_views VALUES (?,?,?,?,?,?,?,?)",
                                   (123, DAY, "2026-10-10", "2026-10-10T09:00:00", 10, 1, 2, 0))
                store.conn.commit()
                store.add_view_sample(123, DAY, "2026-10-11", 20, 2, 3, 0)
                old, new = store.view_samples()
                self.assertEqual(old["sampled_at"], "2026-10-10T09:00:00")
                timestamp = datetime.fromisoformat(new["sampled_at"])
                self.assertIsNotNone(timestamp.tzinfo)
                self.assertEqual(timestamp.utcoffset().total_seconds(), 0)
            finally:
                store.close()


class TestResolvedDailyPublication(unittest.TestCase):
    """Real temporary state/report joins; publication and database readback stay offline."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "fixture.sqlite"
        self.store = Store(self.db_path)
        self.addCleanup(self.store.close)
        self.store.save_daily_v2(DAY, [], "offline.md", post_title="原样标题")
        self.confirmed = datetime(2026, 10, 9, 9, tzinfo=timezone.utc)
        self.lookup_time = self.confirmed + timedelta(hours=5)
        self.lookup_started = False
        self.marker_during_lookup = None
        self.published_during_lookup = None
        self.content_meta = {"article_type": "mechanism", "learning_question": "为何缓存有效？",
                             "published_at": "1999-01-01T00:00:00+00:00",
                             "source_hash": "offline-source-hash", "draft_hash": "offline-draft-hash",
                             "evidence_count": 3}

    def publication_logs(self):
        return [row["detail"] for row in self.store.run_log_since("", "daily")
                if row["status"] == "ok" and row["detail"].get("detail") == "published"]

    def publish(self, *, post_id=None, resolved_id=123, lookup_error=None, visibility=False, task="daily"):
        def find(user_id, title, since):
            if task == "daily":
                # The confirmed success and its metadata must survive a failed/slow readback.
                self.marker_during_lookup = self.store.conn.execute(
                    "SELECT value FROM meta WHERE key=?", ("daily_posted:" + DAY,)).fetchone()[0]
                self.published_during_lookup = self.publication_logs()
            self.lookup_started = True
            if lookup_error:
                raise lookup_error
            return resolved_id

        def now(tz=None):
            stamp = self.lookup_time if self.lookup_started else self.confirmed
            return stamp if tz is not None else stamp.replace(tzinfo=None)

        with patch("main.settings.daily_publish", True), \
             patch("main.settings.publish_weekend", True), \
             patch("main.settings.market_refresh_token", "offline-placeholder"), \
             patch("main.settings.market_user_telephone", "offline-user"), \
             patch("app.publisher.publish_md", return_value=(post_id, None, None)) as publisher, \
             patch("main.marketdb.user_id_of", return_value=7), \
             patch("main.marketdb.find_post_id", side_effect=find) as lookup, \
             patch("main.marketdb.post_is_private", return_value=visibility) as visible, \
             patch("main.datetime") as clock, \
             patch("main._verify_published", wraps=main._verify_published) as verify:
            clock.now.side_effect = now
            success = main._publish_md(self.store, task, DAY, "原样标题", BODY,
                                       dry_run=False, content_meta=self.content_meta)
        self.assertIs(success, True)
        publisher.assert_called_once_with("原样标题", BODY, private=False)
        verify.assert_called_once_with(self.store, task, post_id, "原样标题")
        return lookup, visible

    def assert_forced_rerun_skips(self):
        with patch("main.Store", side_effect=lambda: Store(self.db_path)), \
             patch("main.date") as clock, \
             patch("main._pool_daily_materials", side_effect=AssertionError("already published")), \
             patch("main.editorial.run_pipeline", side_effect=AssertionError("already published")), \
             patch("app.publisher.publish_md", side_effect=AssertionError("must not republish")) as publisher:
            clock.today.return_value = date.fromisoformat(DAY)
            self.assertEqual(main.run_daily(force=True), 0)
        publisher.assert_not_called()

    def test_missing_id_resolves_preserving_metadata_and_aware_report_join(self):
        lookup, visible = self.publish()
        lookup.assert_called_once()
        visible.assert_called_once_with(123)
        self.assertEqual(self.store.daily_post_id(DAY), 123)
        self.assertEqual(self.marker_during_lookup, "0")
        self.assertEqual(self.published_during_lookup, [])
        records = self.publication_logs()
        self.assertEqual(len(records), 1)
        associated = [record for record in records if record["post_id"] == 123]
        self.assertEqual(len(associated), 1)
        detail = associated[0]
        self.assertEqual(detail["published_at"], "2026-10-09T09:00:00+00:00")
        self.assertEqual(detail["time_basis"], "local_confirmation")
        self.assertEqual(detail["content_version"], "research-v1")
        self.assertEqual(detail["source_hash"], "offline-source-hash")
        self.assertEqual(detail["draft_hash"], "offline-draft-hash")
        self.assertEqual(detail["title_hash"], sha256("原样标题".encode("utf-8")).hexdigest())
        self.assertEqual(detail["body_hash"], sha256(BODY.encode("utf-8")).hexdigest())
        self.assertEqual(detail["word_count"], len(BODY))
        self.assertEqual(detail["evidence_count"], 3)
        self.assert_forced_rerun_skips()
        self.store.conn.execute("INSERT INTO post_views VALUES (?,?,?,?,?,?,?,?)",
                                (123, DAY, "2026-10-10", "2026-10-10T09:00:00+00:00", 20, 2, 1, 0))
        self.store.conn.commit()
        self.store.close()
        samples, titles, post_meta = _read_report_data(self.db_path)
        rows = report.report_rows(samples, titles, post_meta)
        self.assertEqual(rows[0]["sample_basis"], "h24_window")
        self.assertEqual(rows[0]["observation_age_hours"], 24.0)
        self.assertEqual(rows[0]["learning_question"], "为何缓存有效？")
        self.assertEqual(rows[0]["article_type"], "mechanism")
        self.assertEqual(rows[0]["title"], "原样标题")
        self.assertIn("research-v1 / 21–27h 窗口 / 本地确认", report.summary(rows, DAY))

    def test_failed_lookup_keeps_zero_marker_and_prevents_forced_rerun(self):
        lookup, visible = self.publish(resolved_id=None)
        lookup.assert_called_once()
        visible.assert_not_called()
        self.assertTrue(self.store.is_daily_posted(DAY))
        self.assertIsNone(self.store.daily_post_id(DAY))
        self.assertEqual(len(self.publication_logs()), 1)
        self.assertIsNone(self.publication_logs()[0]["post_id"])
        self.assert_forced_rerun_skips()

    def test_lookup_exception_keeps_confirmed_success_and_zero_marker(self):
        self.publish(lookup_error=RuntimeError("offline lookup unavailable"))
        self.assertTrue(self.store.is_daily_posted(DAY))
        self.assertIsNone(self.store.daily_post_id(DAY))
        self.assertEqual(len(self.publication_logs()), 1)
        self.assert_forced_rerun_skips()

    def test_returned_positive_id_uses_one_readback_without_lookup_or_extra_publication_log(self):
        lookup, visible = self.publish(post_id=123)
        lookup.assert_not_called()
        visible.assert_called_once_with(123)
        self.assertEqual(self.store.daily_post_id(DAY), 123)
        self.assertEqual(len(self.publication_logs()), 1)

    def test_star_missing_id_keeps_its_original_marker_and_log(self):
        self.publish(task="star")
        self.assertTrue(self.store.is_star_posted(DAY))
        self.assertEqual(self.store.conn.execute(
            "SELECT value FROM meta WHERE key=?", ("star_posted:" + DAY,)).fetchone()[0], "0")
        self.assertFalse(self.store.is_daily_posted(DAY))
        logs = [row["detail"] for row in self.store.run_log_since("", "star")
                if row["detail"].get("detail") == "published"]
        self.assertEqual(len(logs), 1)
        self.assertIsNone(logs[0]["post_id"])

    def test_resolved_id_survives_private_unknown_or_failed_visibility(self):
        for value in (True, None, RuntimeError("offline visibility unavailable")):
            with self.subTest(visibility=value), \
                 patch("main.marketdb.user_id_of", return_value=7), \
                 patch("main.marketdb.find_post_id", return_value=123), \
                 patch("main.marketdb.post_is_private",
                       side_effect=value if isinstance(value, Exception) else None,
                       return_value=value):
                self.assertEqual(main._verify_published(self.store, "daily", None, "原样标题"), 123)

    def test_invalid_lookup_ids_are_not_associated_or_checked_for_visibility(self):
        for value in (0, -1, True, "123"):
            with self.subTest(post_id=value), \
                 patch("main.marketdb.user_id_of", return_value=7), \
                 patch("main.marketdb.find_post_id", return_value=value), \
                 patch("main.marketdb.post_is_private") as visible:
                self.assertIsNone(main._verify_published(self.store, "daily", None, "原样标题"))
                visible.assert_not_called()


class TestPublicationLogRetention(unittest.TestCase):
    def test_published_metadata_survives_500_cutoff_and_expires_with_daily_window(self):
        today = date(2026, 10, 12)
        cutoff = today - timedelta(days=90)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "fixture.db")
            try:
                def insert(task, status, detail, day=today):
                    return store.conn.execute(
                        "INSERT INTO run_log(ts,task,status,detail) VALUES(?,?,?,?)",
                        (day.isoformat() + "T09:00:00", task, status, detail)).lastrowid

                metadata = {"detail": "published", "post_id": 123, "content_version": "research-v1",
                            "published_at": cutoff.isoformat() + "T01:00:00+00:00"}
                protected = insert("daily", "ok", json.dumps(metadata), cutoff)
                ordinary = [insert("ingest", "ok", json.dumps({"sequence": i})) for i in range(520)]
                # Expired publication rows must disappear even if their id is in the newest 500.
                expired = insert("daily", "ok", json.dumps({"detail": "published", "post_id": 99}),
                                 cutoff - timedelta(days=1))
                # Similar or malformed logs are ordinary, not publication exemptions.
                ordinary += [insert("star", "ok", '{"detail":"published"}'),
                             insert("daily", "degraded", '{"detail":"published"}'),
                             insert("daily", "ok", "invalid JSON"),
                             insert("daily", "ok", '["published"]')]
                store.conn.commit()
                with patch("app.store.date") as clock, \
                     patch("app.store.settings.daily_retain_days", 90), \
                     patch("app.store.settings.run_log_retain", 500):
                    clock.today.return_value = today
                    counts = store.cleanup()
                remaining = store.conn.execute("SELECT id,detail FROM run_log ORDER BY id").fetchall()
                ids = {row["id"] for row in remaining}
                self.assertEqual(ids, {protected, *ordinary[-500:]})
                self.assertNotIn(expired, ids)
                self.assertEqual(len(remaining), 501)
                self.assertEqual(counts["runlog_deleted"], 25)
                self.assertEqual(json.loads(next(row["detail"] for row in remaining
                                                if row["id"] == protected)), metadata)
                # Passing the next day makes the boundary publication expire too.
                with patch("app.store.date") as clock, \
                     patch("app.store.settings.daily_retain_days", 90), \
                     patch("app.store.settings.run_log_retain", 500):
                    clock.today.return_value = today + timedelta(days=1)
                    self.assertEqual(store.cleanup()["runlog_deleted"], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM run_log").fetchone()[0], 500)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

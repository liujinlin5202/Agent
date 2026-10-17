# -*- coding: utf-8 -*-
"""审计状态盘单测：追加留痕 / 冷却窗口 / 损坏行容错。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import state


def _row(pid: int, hours_ago: float) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"ts": ts.isoformat(), "mode": "daily", "post_id": pid,
            "title": "t", "featured": False, "reason": "r"}


class TestState(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_append_and_load(self):
        self.assertEqual(state.append_decisions(self.dir, []), 0)
        n = state.append_decisions(self.dir, [_row(1, 0), _row(2, 0)])
        self.assertEqual(n, 2)
        rows = state.load_decisions(self.dir)
        self.assertEqual([r["post_id"] for r in rows], [1, 2])

    def test_rejudge_window(self):
        state.append_decisions(self.dir, [_row(1, 1), _row(2, 30), _row(3, 5)])
        cooling = state.recently_judged(self.dir, rejudge_hours=20)
        self.assertEqual(cooling, {1, 3})
        self.assertNotIn(2, cooling)

    def test_corrupt_lines_skipped(self):
        p = state.decisions_path(self.dir)
        p.write_text('{"ts": "2026-10-14T00:00:00+00:00", "post_id": 7}\n'
                     '半行损坏{{\n\n', encoding="utf-8")
        rows = state.load_decisions(self.dir)
        self.assertEqual(len(rows), 1)

    def test_missing_dir(self):
        cooling = state.recently_judged(Path(tempfile.mkdtemp()) / "nope", 20)
        self.assertEqual(cooling, set())


class TestPendingAndMark(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _rows(self):
        return [
            {"ts": "2026-10-17T00:00:00+00:00", "post_id": 1, "featured": True},
            {"ts": "2026-10-17T00:00:01+00:00", "post_id": 2, "featured": False},
            {"ts": "2026-10-17T00:00:02+00:00", "post_id": 3, "featured": True, "marked": True},
            {"ts": "2026-10-17T00:00:03+00:00", "post_id": 1, "featured": True},
        ]

    def test_pending_dedup_and_scope(self):
        state.append_decisions(self.dir, self._rows())
        # 1=featured 未标记（两行去重）、3=已标记不算欠账、2=未入选
        self.assertEqual(state.pending_featured(self.dir), [1])

    def test_mark_backfill_converges(self):
        state.append_decisions(self.dir, self._rows())
        self.assertEqual(state.mark_decisions(self.dir, {1}, True), 2)
        self.assertEqual(state.pending_featured(self.dir), [])
        # 重放幂等
        self.assertEqual(state.mark_decisions(self.dir, {1}, True), 0)

    def test_mark_leaves_nonfeatured_alone(self):
        state.append_decisions(self.dir, self._rows())
        state.mark_decisions(self.dir, {2}, True)
        rows = state.load_decisions(self.dir)
        self.assertIsNone(rows[1].get("marked"))


if __name__ == "__main__":
    unittest.main()

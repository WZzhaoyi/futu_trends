from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from db_engine.screener import ConsistencyError, save, verify


def result(date, market="A", thresholds=None, candidates=()):
    out = {"date": date, "strategy": "pr", "market": market,
           "generated_at": f"{date} 10:00:00", "l1_count": 3,
           "returned": len(candidates), "l2_reject_reasons": {"roe": 1},
           "candidates": list(candidates)}
    if thresholds is not None:
        out["thresholds"] = thresholds
    return out


class SqliteSaveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sub" / "screener.db"
        self.uri = f"sqlite:///{self.path}"

    def sql(self, sql, *args):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(sql, args).fetchall()

    def test_rows_read_back_by_version_fields_in_rank_order(self):
        """外部 agent 按 (strategy, ver) 取 fields 读 rows：列须对齐、行序即排名。"""
        save(self.uri, [result("20260820", thresholds={"PR_MAX": 0.5}, candidates=[
            {"code": "SZ.000002", "name": "B", "total_market_val": 1.8e10 + 0.6,
             "pe_ttm_ratio": 4.3482, "return_on_equity_rate": None},
            {"code": "SH.600001", "name": "A", "pe_ttm_ratio": 9.0},
        ])])

        (fields, rows, stats), = self.sql(
            "SELECT v.fields, r.rows, r.stats FROM screener_runs r "
            "JOIN screener_versions v USING (strategy, ver) WHERE r.id = '20260820:pr:A'")
        rows = [dict(zip(json.loads(fields), row)) for row in json.loads(rows)]

        self.assertEqual(json.loads(stats), {"l2_reject_reasons": {"roe": 1}})
        self.assertEqual([r["code"] for r in rows], ["SZ.000002", "SH.600001"])
        self.assertEqual(rows[0]["mv"], 18000000001)
        self.assertEqual(rows[0]["pe_ttm"], 4.35)
        self.assertIsNone(rows[0]["roe"])
        self.assertIsNone(rows[1]["mv"])

    def test_versions_stored_once_and_split_on_threshold_change(self):
        """同一门槛跨日期/市场只存一份；改门槛产生新 ver；无快照的结果也有版本；重跑整条替换。"""
        old, new = {"PR_MAX": 0.5, "ROE_MIN": 8.0}, {"PR_MAX": 0.6, "ROE_MIN": 8.0}
        save(self.uri, [result("20261013", "A", old), result("20261013", "HK", old),
                        result("20261014", "A", dict(reversed(old.items()))),
                        result("20261015", "A", new), result("20261012", "A")])
        save(self.uri, [result("20261015", "A", new)])

        runs = dict(self.sql("SELECT id, ver FROM screener_runs"))
        versions = dict(self.sql("SELECT ver, thresholds FROM screener_versions"))

        self.assertEqual(len(runs), 5)
        self.assertEqual(runs["20261013:pr:A"], runs["20261013:pr:HK"])
        self.assertEqual(runs["20261013:pr:A"], runs["20261014:pr:A"])
        self.assertNotEqual(runs["20261013:pr:A"], runs["20261015:pr:A"])
        self.assertEqual(len(versions), 3)
        self.assertIsNone(versions[runs["20261012:pr:A"]])
        self.assertEqual(verify(self.uri), [])

    def test_external_edits_are_detected(self):
        save(self.uri, [result("20261013", "A", {"PR_MAX": 0.5},
                               candidates=[{"code": "SZ.000002"}])])
        self.sql("UPDATE screener_versions SET thresholds = '{\"PR_MAX\": 0.9}'")
        self.sql("UPDATE screener_runs SET rows = '[[\"SZ.000002\"]]'")

        problems = verify(self.uri)
        with self.assertRaises(ConsistencyError):
            save(self.uri, [result("20261014", "A", {"PR_MAX": 0.5})])

        self.assertEqual(len(problems), 2)
        self.assertIn("被外部改动", problems[0])
        self.assertIn("行宽", problems[1])


if __name__ == "__main__":
    unittest.main()

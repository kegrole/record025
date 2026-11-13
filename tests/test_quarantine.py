"""quarantine 工具的判定口径测试（unittest，仅标准库）。

每条 test 对应需求里的一条判定口径：
1. 夹具形状：100 条记录、8 条坏（3 缺必填 / 3 越界 / 2 格式）、5 可重试 + 3 永久坏
2. 跑完 valid 恰好 95 条、quarantine 恰好 3 条
3. 5 条可重试记录的重取次数 > 0 且修正后出现在 valid
4. 隔离记录含 record_id/rule_id/reason/raw，raw 可还原，rule_id 互不相同
5. 跨字段一致性规则（start<end、total=sum(parts)）真的生效
6. 幂等：同一输入再跑一遍，两表行数都不增加
7. 规则由配置文件驱动：换一份规则，同一条记录判定跟着变
8. 报告里每条规则命中次数与隔离记录对得上
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quarantine.cli import run
from quarantine.rules import load_rules
from quarantine.validate import Engine

STATUSES = ["active", "pending", "closed"]

RULES_YAML = """
id_field: id
max_retries: 2
source: source.jsonl
rules:
  - id: id_required
    field: id
    check: required
    on_fail: permanent
  - id: name_required
    field: name
    check: required
    on_fail: permanent
  - id: email_required
    field: email
    check: required
    on_fail: retry
  - id: status_required
    field: status
    check: required
    on_fail: retry
  - id: amount_required
    field: amount
    check: required
    on_fail: retry
  - id: quantity_required
    field: quantity
    check: required
    on_fail: retry
  - id: amount_type
    field: amount
    check: type
    type: number
    on_fail: permanent
  - id: quantity_type
    field: quantity
    check: type
    type: integer
    on_fail: permanent
  - id: amount_range
    field: amount
    check: range
    min: 0
    max: 10000
    on_fail: retry
  - id: quantity_range
    field: quantity
    check: range
    min: 1
    max: 100
    on_fail: permanent
  - id: status_enum
    field: status
    check: enum
    values: [active, pending, closed]
    on_fail: retry
  - id: end_after_start
    check: cross
    op: lt
    fields: [start, end]
    on_fail: retry
  - id: total_matches_parts
    check: sum_eq
    field: total
    sum_of: [subtotal, tax]
    tolerance: 0.01
    on_fail: retry
  - id: email_format
    field: email
    check: regex
    pattern: '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'
    on_fail: permanent
"""


def good_record(i):
    amount = round(((i * 37) % 500) + 0.5, 2)
    tax = round(amount * 0.1, 2)
    return {
        "id": "r%03d" % i,
        "name": "user%03d" % i,
        "email": "user%03d@example.com" % i,
        "status": STATUSES[i % 3],
        "amount": amount,
        "quantity": (i % 50) + 1,
        "start": "2026-01-%02d" % ((i % 28) + 1),
        "end": "2026-02-%02d" % ((i % 28) + 1),
        "subtotal": amount,
        "tax": tax,
        "total": round(amount + tax, 2),
    }


def build_records():
    """返回 (records_by_index, source_records, bad_meta)。

    bad_meta: record_id -> (category, expected_rule_id, retryable)
    category 用于核对 3 缺必填 / 3 越界 / 2 格式 的夹具构成。
    """
    records = {i: good_record(i) for i in range(1, 99)}  # r001..r098
    bad = {}

    def tamper(i, category, rule_id, retryable, **changes):
        rec = dict(records[i])
        for key, value in changes.items():
            if value is None:
                rec.pop(key, None)
            else:
                rec[key] = value
        records[i] = rec
        bad[rec["id"]] = (category, rule_id, retryable)

    tamper(10, "missing", "name_required", False, name=None)
    tamper(11, "missing", "email_required", True, email=None)
    tamper(12, "missing", "status_required", True, status=None)
    tamper(20, "range", "amount_range", True, amount=-5)
    tamper(21, "range", "amount_range", True, amount=99999)
    tamper(22, "range", "quantity_range", False, quantity=0)
    tamper(30, "format", "email_format", False, email="not-an-email")
    tamper(31, "format", "end_after_start", True,
           start="2026-03-10", end="2026-03-01", email="bad.email")

    source = [good_record(i) for i in (11, 12, 20, 21, 31)]
    return records, source, bad


def write_fixture(root):
    records, source, bad = build_records()
    input_path = os.path.join(root, "records.jsonl")
    lines = [records[i] for i in sorted(records)]
    lines.append(dict(records[1]))  # 完全重复，验证幂等
    lines.append(dict(records[2]))
    with open(input_path, "w", encoding="utf-8") as fh:
        for rec in lines:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    source_path = os.path.join(root, "source.jsonl")
    with open(source_path, "w", encoding="utf-8") as fh:
        for rec in source:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    rules_path = os.path.join(root, "rules.yaml")
    with open(rules_path, "w", encoding="utf-8") as fh:
        fh.write(RULES_YAML)
    return input_path, rules_path, records, bad


class PipelineTestCase(unittest.TestCase):
    """共享一次流水线运行结果的测试基类。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="quarantine-test-")
        cls.input_path, cls.rules_path, cls.records, cls.bad = write_fixture(cls.tmp)
        cls.db_path = os.path.join(cls.tmp, "q.db")
        cls.out_path = os.path.join(cls.tmp, "report.json")
        cls.report = run(cls.input_path, cls.db_path, cls.rules_path, cls.out_path)
        cls.engine = Engine(load_rules(cls.rules_path).rules)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def query(self, sql, args=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


class TestFixtureShape(PipelineTestCase):
    """口径 1：100 条记录，8 条坏（3/3/2），5 可重试 + 3 永久坏。"""

    def test_fixture_shape(self):
        with open(self.input_path, encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        self.assertEqual(100, len(lines))
        self.assertEqual(8, len(self.bad))

        categories = [meta[0] for meta in self.bad.values()]
        self.assertEqual(3, categories.count("missing"))
        self.assertEqual(3, categories.count("range"))
        self.assertEqual(2, categories.count("format"))

        retryable = 0
        permanent = 0
        by_id = {rec["id"]: rec for rec in self.records.values()}
        for record_id, (_, expected_rule, expected_retryable) in self.bad.items():
            failure = self.engine.validate(by_id[record_id])
            self.assertIsNotNone(failure, record_id)
            self.assertEqual(expected_rule, failure.rule_id, record_id)
            self.assertEqual(expected_retryable, failure.retryable, record_id)
            if failure.retryable:
                retryable += 1
            else:
                permanent += 1
        self.assertEqual(5, retryable)
        self.assertEqual(3, permanent)


class TestCounts(PipelineTestCase):
    """口径 2：valid 恰好 95 条，quarantine 恰好 3 条。"""

    def test_counts(self):
        self.assertEqual(95, self.query("SELECT COUNT(*) FROM valid")[0][0])
        self.assertEqual(3, self.query("SELECT COUNT(*) FROM quarantine")[0][0])
        self.assertEqual(95, self.report["valid"])
        self.assertEqual(3, self.report["quarantine"])
        self.assertEqual(100, self.report["total"])


class TestRetry(PipelineTestCase):
    """口径 3：5 条可重试记录重取次数 > 0，修正后出现在 valid。"""

    def test_retries_actually_happened(self):
        retryable_ids = sorted(
            rid for rid, meta in self.bad.items() if meta[2]
        )
        self.assertEqual(5, len(retryable_ids))
        rows = dict(
            self.query(
                "SELECT record_id, retries FROM valid WHERE record_id IN (%s)"
                % ",".join("?" * len(retryable_ids)),
                retryable_ids,
            )
        )
        self.assertEqual(set(retryable_ids), set(rows))
        for record_id in retryable_ids:
            self.assertGreater(rows[record_id], 0, record_id)
        self.assertEqual(5, self.report["retry_attempts"])

    def test_refetched_records_store_corrected_values(self):
        # r020 原始 amount=-5，回源修正后应按修正值入库
        data = self.query("SELECT data FROM valid WHERE record_id = 'r020'")[0][0]
        self.assertEqual(good_record(20)["amount"], json.loads(data)["amount"])


class TestQuarantineRows(PipelineTestCase):
    """口径 4：隔离记录字段齐全、raw 可还原、rule_id 互不相同。"""

    def test_quarantine_fields_and_raw(self):
        rows = self.query(
            "SELECT record_id, rule_id, reason, raw, retries FROM quarantine"
        )
        self.assertEqual(3, len(rows))
        by_id = {rec["id"]: rec for rec in self.records.values()}
        for record_id, rule_id, reason, raw, retries in rows:
            self.assertTrue(record_id)
            self.assertTrue(rule_id)
            self.assertTrue(reason)
            self.assertTrue(raw)
            self.assertIsInstance(retries, int)
            # raw 能还原成原始那条坏记录
            self.assertEqual(by_id[record_id], json.loads(raw))

    def test_quarantine_rule_ids_distinct_and_cover_three_kinds(self):
        rule_ids = [row[0] for row in self.query("SELECT rule_id FROM quarantine")]
        self.assertEqual(3, len(set(rule_ids)))
        # 分别对应缺字段、越界、格式三种
        self.assertEqual(
            {"name_required", "quantity_range", "email_format"}, set(rule_ids)
        )


class TestCrossField(PipelineTestCase):
    """口径 5：跨字段一致性规则真的生效。"""

    def test_end_must_be_after_start(self):
        record = good_record(50)
        record["start"] = "2026-03-10"
        record["end"] = "2026-03-01"
        failure = self.engine.validate(record)
        self.assertIsNotNone(failure)
        self.assertEqual("end_after_start", failure.rule_id)
        # 可重试还是永久坏由规则配置决定，demo 规则里是 retry
        self.assertTrue(failure.retryable)

    def test_total_must_equal_sum_of_parts(self):
        record = good_record(51)
        record["total"] = round(record["subtotal"] + record["tax"] + 5, 2)
        failure = self.engine.validate(record)
        self.assertIsNotNone(failure)
        self.assertEqual("total_matches_parts", failure.rule_id)


class TestIdempotency(PipelineTestCase):
    """口径 6：同一份输入再跑一遍，两表行数都不增加。"""

    def test_rerun_is_idempotent(self):
        before_valid = self.query("SELECT COUNT(*) FROM valid")[0][0]
        before_quarantine = self.query("SELECT COUNT(*) FROM quarantine")[0][0]
        report2 = run(self.input_path, self.db_path, self.rules_path,
                      os.path.join(self.tmp, "report2.json"))
        after_valid = self.query("SELECT COUNT(*) FROM valid")[0][0]
        after_quarantine = self.query("SELECT COUNT(*) FROM quarantine")[0][0]
        self.assertEqual(before_valid, after_valid)
        self.assertEqual(before_quarantine, after_quarantine)
        self.assertEqual(0, report2["valid"])
        self.assertEqual(0, report2["quarantine"])


class TestRulesDriveJudgement(PipelineTestCase):
    """口径 7：换一份规则文件，同一条记录的判定结果跟着变。"""

    def test_same_record_different_verdict(self):
        record = good_record(5)  # amount = 185.5
        self.assertIsNone(self.engine.validate(record))

        stricter = RULES_YAML.replace("max: 10000", "max: 100")
        strict_path = os.path.join(self.tmp, "rules_strict.yaml")
        with open(strict_path, "w", encoding="utf-8") as fh:
            fh.write(stricter)
        strict_engine = Engine(load_rules(strict_path).rules)
        failure = strict_engine.validate(record)
        self.assertIsNotNone(failure)
        self.assertEqual("amount_range", failure.rule_id)

    def test_pipeline_counts_follow_rules(self):
        stricter = RULES_YAML.replace("max: 10000", "max: 100")
        strict_path = os.path.join(self.tmp, "rules_strict2.yaml")
        with open(strict_path, "w", encoding="utf-8") as fh:
            fh.write(stricter)
        report = run(
            self.input_path,
            os.path.join(self.tmp, "strict.db"),
            strict_path,
            os.path.join(self.tmp, "strict_report.json"),
        )
        self.assertNotEqual(95, report["valid"])
        self.assertLess(report["valid"], 95)
        self.assertGreater(report["quarantine"], 3)


class TestReport(PipelineTestCase):
    """口径 8：报告里每条规则的命中次数与实际隔离记录对得上。"""

    def test_rule_hits_match_quarantine(self):
        rows = self.query("SELECT rule_id, COUNT(*) FROM quarantine GROUP BY rule_id")
        expected = {rule_id: count for rule_id, count in rows}
        self.assertEqual(expected, self.report["rule_hits"])
        self.assertEqual(
            self.query("SELECT COUNT(*) FROM quarantine")[0][0],
            sum(self.report["rule_hits"].values()),
        )

    def test_report_file_written(self):
        with open(self.out_path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(self.report, on_disk)


if __name__ == "__main__":
    unittest.main()

"""命令行入口与处理流水线：校验 -> 可重试的回源重取 -> 入库/隔离 -> 报告。"""
import argparse
import hashlib
import json

from .report import build_report, write_report
from .retry import JsonlSource
from .rules import load_rules
from .store import Store
from .validate import Engine


def _process_record(record, record_id, engine, source, max_retries, store, stats, rule_hits):
    current = record
    retries = 0
    while True:
        failure = engine.validate(current)
        if failure is None:
            store.add_valid(record_id, current, retries)
            stats["valid"] += 1
            if retries:
                stats["retried_records"] += 1
            return
        if failure.retryable and source is not None and retries < max_retries:
            retries += 1
            stats["retry_attempts"] += 1
            fetched = source.fetch(record_id)
            if fetched is not None:
                current = fetched
            continue
        store.add_quarantine(record_id, failure.rule_id, failure.reason, record, retries)
        stats["quarantine"] += 1
        rule_hits[failure.rule_id] = rule_hits.get(failure.rule_id, 0) + 1
        return


def run(input_path, db_path, rules_path, out_path):
    """跑一遍完整流水线，返回报告 dict（同时写入 out_path）。"""
    ruleset = load_rules(rules_path)
    engine = Engine(ruleset.rules)
    store = Store(db_path)
    source = None
    if ruleset.source_path:
        source = JsonlSource(ruleset.source_path, ruleset.id_field)
    stats = {
        "total": 0,
        "valid": 0,
        "quarantine": 0,
        "duplicates": 0,
        "retried_records": 0,
        "retry_attempts": 0,
    }
    rule_hits = {}
    try:
        with open(input_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                stats["total"] += 1
                record = json.loads(line)
                record_id = record.get(ruleset.id_field)
                if record_id is None:
                    record_id = "noid-" + hashlib.sha1(
                        line.encode("utf-8")
                    ).hexdigest()[:16]
                record_id = str(record_id)
                if store.has(record_id):
                    stats["duplicates"] += 1
                    continue
                _process_record(
                    record, record_id, engine, source, ruleset.max_retries,
                    store, stats, rule_hits,
                )
    finally:
        store.close()
    report = build_report(stats, rule_hits)
    write_report(report, out_path)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="quarantine",
        description="校验采集数据：通过的进 valid 表，永久坏进 quarantine 表，可重试的回源重取。",
    )
    parser.add_argument("--input", required=True, help="待校验数据（JSONL）")
    parser.add_argument("--db", required=True, help="sqlite 数据库路径")
    parser.add_argument("--rules", required=True, help="规则配置文件")
    parser.add_argument("--out", required=True, help="JSON 报告输出路径")
    args = parser.parse_args(argv)
    report = run(args.input, args.db, args.rules, args.out)
    print(
        "summary: read={total} valid={valid} quarantined={quarantine} "
        "retries={retry_attempts}".format(**report)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

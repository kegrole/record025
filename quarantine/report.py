"""JSON 报告：各类计数 + 每条规则的命中次数。"""
import json
import os


def build_report(stats, rule_hits):
    report = dict(stats)
    report["rule_hits"] = dict(sorted(rule_hits.items()))
    return report


def write_report(report, path):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

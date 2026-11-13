# quarantine：采集数据校验、隔离与回源重取

只用 Python 标准库。流水线：读入 JSONL -> 按规则校验 -> 通过的进 `valid` 表；
永久坏的进 `quarantine` 表；可重试的回源重取（有上限），重取结果重新过校验，
成功按修正值入库并记录重取次数，超限转永久坏进隔离区。同一条记录重复送入不会重复入库。

## 运行

```bash
python -m quarantine.cli --input demo/records.jsonl --db out/q.db --rules demo/rules.yaml --out out/report.json
```

跑完在 stdout 打印一行摘要（读入条数、通过条数、隔离条数、重取次数）：

```
summary: read=100 valid=95 quarantined=3 retries=5
```

测试：

```bash
cd tests && python -m unittest test_quarantine -v
# 或在仓库根目录：python -m unittest tests.test_quarantine -v
```

## 规则文件格式

规则写在一个可读的 YAML 子集配置里（标准库没有 YAML，`quarantine/rules.py`
自己实现了解析：注释、缩进映射、`- ` 列表、`[a, b]` 行内列表、标量）。

```yaml
id_field: id            # 记录主键字段，幂等与回源都按它对齐
max_retries: 2          # 可重试记录的回源重取上限
source: source.jsonl    # 回源取数的本地数据源（JSONL），相对本文件解析
rules:
  - id: amount_range    # 规则 id，隔离表与报告用它归因
    field: amount
    check: range
    min: 0
    max: 10000
    on_fail: retry      # retry=可重试；permanent=永久坏
```

规则按声明顺序逐条检查，命中第一个失败即停，该规则就是这条记录的归因规则。
非 `required` 规则在字段缺失时跳过（缺失交给 `required` 规则判定）。

支持的 `check`：

| check      | 额外字段                          | 含义 |
|------------|-----------------------------------|------|
| `required` | `field`                           | 字段必须存在且非 null、非空串 |
| `type`     | `field`, `type`（string/integer/number/boolean） | 类型校验 |
| `range`    | `field`, `min`/`max`（可省）       | 数值闭区间 |
| `regex`    | `field`, `pattern`                | 正则匹配 |
| `enum`     | `field`, `values`                 | 枚举取值 |
| `cross`    | `fields: [a, b]`, `op`（lt/le/gt/ge/eq/ne） | 跨字段比较，如结束时间必须晚于开始时间 |
| `sum_eq`   | `field`, `sum_of: [...]`, `tolerance`（可省） | 总额必须等于各分项之和 |

## 可重试 vs 永久坏

每条规则用 `on_fail` 声明失败归类：

- `retry`：认为是上游暂时给了坏数据，按 `source` 配置回源重取（按 `id_field`
  查找该记录的最新版本）。重取结果重新从第一条规则过校验；仍失败且还可重试，
  就再取，直到 `max_retries` 上限。超限后转永久坏进隔离区。
- `permanent`：直接进隔离区，不重取。

重取成功（修正后通过校验）的记录按**修正后的值**进 `valid` 表，并记录重取次数。

## 隔离与存储语义

sqlite 两张表：

- `valid(record_id, retries, data)`：通过的记录，`data` 是（可能被修正的）记录 JSON。
- `quarantine(record_id, rule_id, reason, raw, retries)`：隔离记录，含原始数据
  `raw`（可 `json.loads` 还原成原始那条记录）、命中的规则 `rule_id`、失败原因
  `reason`、隔离前已发生的重取次数 `retries`。

幂等：两表都以 `record_id` 为主键，写入用 `INSERT OR IGNORE`，处理前先查存在性；
同一条记录（同一文件里的重复行，或整份输入再跑一遍）不会重复入库、不会重复隔离。

## 报告

`--out` 指定的 JSON 报告包含：`total`（读入）、`valid`、`quarantine`、
`duplicates`（幂等跳过）、`retried_records`（发生过重取的记录数）、
`retry_attempts`（重取总次数）、`rule_hits`（每条规则命中次数，与隔离记录一一对账）。

## demo 数据说明

`demo/records.jsonl` 共 100 行：98 条唯一记录（90 条好 + 8 条坏：3 缺必填、
3 数值越界、2 格式不对，其中 1 条格式坏的记录同时 start>end 触发跨字段规则）
+ 2 条完全重复行。8 条坏记录里 5 条可重试（`demo/source.jsonl` 里有它们的修正版，
重取后全部救回）、3 条永久坏进隔离区。所以跑完 `valid` 恰好 95 条、
`quarantine` 恰好 3 条。

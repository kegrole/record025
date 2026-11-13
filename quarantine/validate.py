"""校验引擎：按规则声明顺序逐条检查记录，返回第一个失败。

非 required 规则在字段缺失时一律跳过（缺失由 required 规则负责），
这样每条记录只被最具体的那条规则归因一次。
"""
import operator
import re


class Failure:
    """一次校验失败：命中的规则 id、失败原因、是否可重试。"""

    __slots__ = ("rule_id", "reason", "retryable")

    def __init__(self, rule_id, reason, retryable):
        self.rule_id = rule_id
        self.reason = reason
        self.retryable = retryable

    def __repr__(self):
        return "Failure(rule_id=%r, reason=%r, retryable=%r)" % (
            self.rule_id,
            self.reason,
            self.retryable,
        )


_MISSING = object()

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}

_OPS = {
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
    "eq": operator.eq,
    "ne": operator.ne,
}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_required(rule, record):
    field = rule["field"]
    value = record.get(field, _MISSING)
    if value is _MISSING or value is None or value == "":
        return "missing required field %r" % field
    return None


def _check_type(rule, record):
    field = rule["field"]
    value = record.get(field, _MISSING)
    if value is _MISSING or value is None:
        return None
    type_name = rule["type"]
    if not _TYPE_CHECKS[type_name](value):
        return "field %r should be %s, got %s" % (
            field,
            type_name,
            type(value).__name__,
        )
    return None


def _check_range(rule, record):
    field = rule["field"]
    value = record.get(field, _MISSING)
    if value is _MISSING or value is None or not _is_number(value):
        return None
    low = rule.get("min")
    high = rule.get("max")
    if low is not None and value < low:
        return "field %r=%r below min %r" % (field, value, low)
    if high is not None and value > high:
        return "field %r=%r above max %r" % (field, value, high)
    return None


def _check_regex(rule, record):
    field = rule["field"]
    value = record.get(field, _MISSING)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        return "field %r should be a string matching %r" % (field, rule["pattern"])
    if re.match(rule["pattern"], value) is None:
        return "field %r=%r does not match %r" % (field, value, rule["pattern"])
    return None


def _check_enum(rule, record):
    field = rule["field"]
    value = record.get(field, _MISSING)
    if value is _MISSING or value is None:
        return None
    if value not in rule["values"]:
        return "field %r=%r not in %r" % (field, value, rule["values"])
    return None


def _check_cross(rule, record):
    left, right = rule["fields"]
    a = record.get(left, _MISSING)
    b = record.get(right, _MISSING)
    if a is _MISSING or b is _MISSING or a is None or b is None:
        return None
    op_name = rule["op"]
    try:
        ok = _OPS[op_name](a, b)
    except TypeError:
        return "cannot compare %r (%r) with %r (%r)" % (left, a, right, b)
    if not ok:
        return "cross-field check failed: %s %s %s (%r vs %r)" % (
            left,
            op_name,
            right,
            a,
            b,
        )
    return None


def _check_sum_eq(rule, record):
    field = rule["field"]
    parts = list(rule["sum_of"])
    values = [record.get(field, _MISSING)] + [record.get(p, _MISSING) for p in parts]
    if any(v is _MISSING or v is None for v in values):
        return None
    if not all(_is_number(v) for v in values):
        return None
    tolerance = rule.get("tolerance")
    tolerance = 1e-6 if tolerance is None else float(tolerance)
    total = sum(values[1:])
    if abs(values[0] - total) > tolerance:
        return "field %r=%r != sum of %s (=%r)" % (field, values[0], parts, total)
    return None


_CHECKERS = {
    "required": _check_required,
    "type": _check_type,
    "range": _check_range,
    "regex": _check_regex,
    "enum": _check_enum,
    "cross": _check_cross,
    "sum_eq": _check_sum_eq,
}

_REQUIRED_KEYS = {
    "required": ("field",),
    "type": ("field", "type"),
    "range": ("field",),
    "regex": ("field", "pattern"),
    "enum": ("field", "values"),
    "cross": ("fields", "op"),
    "sum_eq": ("field", "sum_of"),
}


class Engine:
    """按顺序应用规则；任何配置错误在构造时就抛出。"""

    def __init__(self, rules):
        self.rules = list(rules)
        for rule in self.rules:
            check = rule.get("check")
            if check not in _CHECKERS:
                raise ValueError(
                    "unknown check %r in rule %r" % (check, rule.get("id"))
                )
            if "id" not in rule:
                raise ValueError("rule missing 'id': %r" % (rule,))
            for key in _REQUIRED_KEYS[check]:
                if key not in rule:
                    raise ValueError("rule %r missing key %r" % (rule["id"], key))
            if check == "type" and rule["type"] not in _TYPE_CHECKS:
                raise ValueError(
                    "rule %r: unknown type %r" % (rule["id"], rule["type"])
                )
            if check == "cross":
                if rule["op"] not in _OPS:
                    raise ValueError(
                        "rule %r: unknown op %r" % (rule["id"], rule["op"])
                    )
                if len(rule["fields"]) != 2:
                    raise ValueError(
                        "rule %r: cross check needs exactly 2 fields" % rule["id"]
                    )
            on_fail = str(rule.get("on_fail", "permanent")).lower()
            if on_fail not in ("retry", "permanent"):
                raise ValueError(
                    "rule %r: on_fail must be retry|permanent" % rule["id"]
                )

    def validate(self, record):
        """返回 None（通过）或第一个 Failure。"""
        for rule in self.rules:
            reason = _CHECKERS[rule["check"]](rule, record)
            if reason is not None:
                retryable = str(rule.get("on_fail", "permanent")).lower() == "retry"
                return Failure(rule["id"], reason, retryable)
        return None

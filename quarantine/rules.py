"""规则配置加载。

配置文件使用一个 YAML 子集（标准库没有 YAML 解析器，这里自己实现）：
支持注释（#）、缩进嵌套的映射、以 "- " 开头的列表、行内列表 [a, b]、
以及标量（整数、浮点、布尔、null、带引号或不带引号的字符串）。

配置结构：
    id_field: id            # 记录主键字段名，默认 "id"
    max_retries: 2          # 可重试记录的回源重取上限，默认 2
    source: source.jsonl    # 回源取数的本地数据源（JSONL），相对规则文件解析
    rules:                  # 规则列表，按声明顺序依次校验，命中即停
      - id: amount_range    # 规则 id（隔离表与报告里用它归因）
        field: amount       # 字段名（cross 规则用 fields，sum_eq 用 field+sum_of）
        check: range        # required|type|range|regex|enum|cross|sum_eq
        on_fail: retry      # retry=可重试回源重取；permanent=永久坏直接隔离
"""
import os


def _strip_comment(line):
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def _split_inline(text):
    parts, buf, quote, depth = [], [], None, 0
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _parse_scalar(text):
    text = text.strip()
    if text == "":
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", "none"):
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_inline(inner)]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _looks_like_map_entry(content):
    return ":" in content and not content.startswith(("'", '"'))


def load_yaml(text):
    """把一个 YAML 子集文本解析成 dict/list/标量。"""
    lines = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.strip()))

    pos = 0

    def parse_block(indent):
        nonlocal pos
        if pos >= len(lines):
            return None
        if lines[pos][1].startswith("-"):
            items = []
            while (
                pos < len(lines)
                and lines[pos][0] == indent
                and lines[pos][1].startswith("-")
            ):
                content = lines[pos][1][1:].strip()
                if not content:
                    pos += 1
                    if pos < len(lines) and lines[pos][0] > indent:
                        items.append(parse_block(lines[pos][0]))
                    else:
                        items.append(None)
                elif _looks_like_map_entry(content):
                    key, _, value = content.partition(":")
                    item = {}
                    pos += 1
                    if value.strip():
                        item[key.strip()] = _parse_scalar(value)
                    elif pos < len(lines) and lines[pos][0] > indent:
                        item[key.strip()] = parse_block(lines[pos][0])
                    else:
                        item[key.strip()] = None
                    while pos < len(lines) and lines[pos][0] > indent:
                        sub_indent = lines[pos][0]
                        sub_key, _, sub_value = lines[pos][1].partition(":")
                        pos += 1
                        if sub_value.strip():
                            item[sub_key.strip()] = _parse_scalar(sub_value)
                        elif pos < len(lines) and lines[pos][0] > sub_indent:
                            item[sub_key.strip()] = parse_block(lines[pos][0])
                        else:
                            item[sub_key.strip()] = None
                    items.append(item)
                else:
                    items.append(_parse_scalar(content))
                    pos += 1
            return items
        mapping = {}
        while (
            pos < len(lines)
            and lines[pos][0] == indent
            and not lines[pos][1].startswith("-")
        ):
            key, _, value = lines[pos][1].partition(":")
            pos += 1
            if value.strip():
                mapping[key.strip()] = _parse_scalar(value)
            elif pos < len(lines) and lines[pos][0] > indent:
                mapping[key.strip()] = parse_block(lines[pos][0])
            else:
                mapping[key.strip()] = None
        return mapping

    if not lines:
        return {}
    return parse_block(lines[0][0])


class Ruleset:
    """一份加载好的规则配置。"""

    def __init__(self, id_field, max_retries, source_path, rules):
        self.id_field = id_field
        self.max_retries = max_retries
        self.source_path = source_path
        self.rules = rules


def load_rules(path):
    """从规则文件加载 Ruleset；source 路径相对规则文件所在目录解析。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = load_yaml(fh.read()) or {}
    if not isinstance(data, dict):
        raise ValueError("rules file must be a mapping at top level")
    rules = data.get("rules") or []
    if not isinstance(rules, list):
        raise ValueError("'rules' must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or "id" not in rule or "check" not in rule:
            raise ValueError("each rule needs at least 'id' and 'check': %r" % (rule,))
    source = data.get("source")
    if source and not os.path.isabs(source):
        source = os.path.join(os.path.dirname(os.path.abspath(path)), source)
    return Ruleset(
        id_field=data.get("id_field", "id"),
        max_retries=int(data.get("max_retries", 2)),
        source_path=source,
        rules=rules,
    )

"""回源重取：本地 JSONL 假数据源，按记录 id 重新取数。"""
import json
import os


class JsonlSource:
    """把一个 JSONL 文件当作上游数据源，按 id 字段索引，可重复查找。"""

    def __init__(self, path, id_field="id"):
        self.path = path
        self.id_field = id_field
        self._index = None

    def _load(self):
        self._index = {}
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                record_id = record.get(self.id_field)
                if record_id is not None:
                    self._index[str(record_id)] = record

    def fetch(self, record_id):
        """返回该 id 的最新记录副本；源里没有则返回 None。"""
        if self._index is None:
            self._load()
        record = self._index.get(str(record_id))
        return dict(record) if record is not None else None

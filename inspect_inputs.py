"""读取原始业务资料，不合并、修订或分析记录。"""

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent / 'data'


def read_tables(root=ROOT):
    definitions = json.loads((root / 'layout.json').read_text(encoding='utf-8'))
    tables = {}
    for name, table in definitions.items():
        with (root / f'{name}.csv').open(encoding='utf-8', newline='') as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != table['header']:
                raise ValueError(f'{name}: CSV 表头与约定不符')
            rows = list(reader)
        if any(len(row) != len(table['header']) or None in row.values() for row in rows):
            raise ValueError(f'{name}: CSV 记录缺列或多列')
        tables[name] = rows
    return tables


if __name__ == '__main__':
    print(json.dumps({name:len(rows) for name,rows in read_tables().items()},ensure_ascii=False,sort_keys=True))

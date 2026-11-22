"""检查经营资料能否按登记的列读取，不解释业务结果。"""
import argparse
import csv
import json
from pathlib import Path


def counts(folder: Path) -> dict[str, int]:
    with (folder / "layout.json").open(encoding="utf-8") as source:
        layout = json.load(source)
    sizes = {}
    for filename, fields in layout.items():
        with (folder / filename).open(newline="", encoding="utf-8") as source:
            reader = csv.reader(source, delimiter="\t", strict=True)
            if next(reader, None) != fields:
                raise ValueError(f"{filename}: 表头不符")
            count = 0
            for line, values in enumerate(reader, start=2):
                if len(values) != len(fields) or any(not value.strip() for value in values):
                    raise ValueError(f"{filename}:{line}: 列数不符或有未标记的空值")
                count += 1
        sizes[filename] = count
    return sizes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, nargs="?", default=Path("data"))
    arguments = parser.parse_args()
    print(json.dumps(counts(arguments.folder), ensure_ascii=False, sort_keys=True))

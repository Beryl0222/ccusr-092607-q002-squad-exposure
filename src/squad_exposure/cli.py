"""命令行入口：领域事件校验与端到端演示。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .contracts import validate_event


def validate(schema_path: str, event_path: str) -> int:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def demo() -> int:
    from .demo import render

    workdir = Path(tempfile.mkdtemp(prefix="squad-exposure-demo-") )
    render(workdir / "events.jsonl")
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "demo":
        return demo()
    if len(sys.argv) == 3:
        return validate(sys.argv[1], sys.argv[2])
    print("用法:", file=sys.stderr)
    print("  python -m squad_exposure.cli <schema.json> <event.json>   校验领域事件", file=sys.stderr)
    print("  python -m squad_exposure.cli demo                         运行端到端演示", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

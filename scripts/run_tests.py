"""隔离、可重复运行的验收入口；保留报告，不清理用户现有目录。"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
test_root = ROOT / ".test-runs" / f"checks-{uuid.uuid4().hex}"
test_root.mkdir(parents=True)
os.environ["TALENTFLOW_TEST_ROOT"] = str(test_root)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

def protected_digests():
    paths = {ROOT / ".env", ROOT / "data/runtime.env"}
    for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
        paths.update((ROOT / "data").glob(pattern))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None for path in sorted(paths)}

before = protected_digests()
sentinel = test_root / "output" / "test_do_not_collect.py"
sentinel.parent.mkdir()
sentinel.write_text("raise AssertionError('隔离 output 哨兵不可收集')", encoding="utf-8")

# Windows 的 0700 特殊 ACL 在受限执行器中会使 pytest 自己无法访问临时目录。
# 仅本进程、此次虚构数据目录继承父目录 ACL，不改用户目录权限。
if os.name == "nt":
    original_mkdir = os.mkdir

    def test_mkdir(path, mode=0o777, *, dir_fd=None):
        if mode == 0o700 and Path(path).resolve().is_relative_to(test_root):
            mode = 0o777
        return original_mkdir(path, mode, dir_fd=dir_fd)

    os.mkdir = test_mkdir

for folder in ("app", "tests", "scripts"):
    for path in (ROOT / folder).rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

import pytest  # noqa: E402

result = pytest.main([
    "tests", "-q", "-p", "no:cacheprovider", "--basetemp", str(test_root / "tmp"),
    "--junitxml", str(test_root / "pytest.xml"), *sys.argv[1:],
])
node = shutil.which("node")
if node:
    theme = subprocess.run([node, "--test", "tests/theme.test.cjs"], cwd=ROOT)
    result = result or theme.returncode
else:
    print("Node.js unavailable: theme tests not executed", file=sys.stderr)
    result = result or 1
print(f"Evidence: {test_root}")
after = protected_digests()
(test_root / "isolation.json").write_text(json.dumps({"before": before, "after": after,
    "unchanged": before == after, "collection_root": "tests", "output_sentinel": str(sentinel)}, indent=2), encoding="utf-8")
if before != after:
    print("受保护文件摘要发生变化，请检查 isolation.json", file=sys.stderr)
    result = result or 1
raise SystemExit(result)

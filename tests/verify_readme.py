"""Verify bili_learn README/SOAK claims against code."""

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
APPJS = (ROOT / "pages" / "monitor" / "app.js").read_text(encoding="utf-8")

issues: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    print(("OK   " if ok else "FAIL ") + name + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        issues.append(name)


# 1. commands both directions
readme_cmds = re.findall(r"`/bilearn (\w+)", README)
code_cmds = re.findall(r'@bilearn\.command\("(\w+)"\)', MAIN)
for cmd in sorted(set(readme_cmds)):
    check(f"cmd /bilearn {cmd} has handler", f'@bilearn.command("{cmd}")' in MAIN)
for cmd in sorted(set(code_cmds)):
    check(f"handler {cmd} documented", f"`/bilearn {cmd}" in README)

# 2. tools
tool_src = (ROOT / "bili" / "tool.py").read_text(encoding="utf-8")
for tool in ("bilibili_read", "bilibili_knowledge"):
    check(f"tool {tool} registered", f'"{tool}"' in tool_src)
    check(f"tool {tool} documented", f"`{tool}`" in README)

# 3. config table vs schema
table = README.split("### 常用配置")[1].split("### 配额与调度说明")[0]
readme_keys = (
    set(re.findall(r"`([a-z][a-z0-9_]+)`", table))
    - {"off", "owner", "strict", "bilibili_read", "bilibili_knowledge"}
)
for key in sorted(readme_keys):
    check(f"config {key} in schema", key in SCHEMA)
missing = sorted(k for k in SCHEMA if k not in readme_keys)
if missing:
    print(f"INFO schema keys not in README table: {missing}")

# 4. quoted defaults vs schema
expected = {
    "unlimited_mode": "false", "run_max_videos": "100", "daily_start_hour": "1",
    "daily_per_keyword": "3", "category_min_score": "80", "subtitle_page_limit": "3",
    "subtitle_max_chars": "12000", "max_duration_minutes": "120",
    "consolidate_threshold": "10", "audit_interval_days": "7", "audit_daily_limit": "20",
    "audit_excerpt_chars": "4000", "query_top_k": "5",
    "daily_token_limit": "0", "soft_token_limit": "0", "single_call_token_cap": "0",
}
for key, expect in expected.items():
    actual = SCHEMA[key]["default"]
    actual_str = str(actual).lower() if isinstance(actual, bool) else str(actual)
    check(f"schema default {key}=={expect}", actual_str == expect, f"schema={actual}")
    check(f"README quotes {key} {expect}", expect in table or expect.lower() in table.lower())

# 5. panel routes vs frontend endpoints
routes = re.findall(r'\("/{0}/(\S+?)"'.format("astrbot_plugin_bili_learn"), MAIN)
for route in sorted(set(routes)):
    check(f"panel api {route} used by frontend", f'"{route}"' in APPJS or f"'{route}'" in APPJS)

# 6. versions
ver_readme = re.search(r"当前版本 `v([^`]+)`", README).group(1)
ver_main = re.search(r'PLUGIN_VERSION = "([^"]+)"', MAIN).group(1)
ver_meta = re.search(r"version: ([^\s]+)", (ROOT / "metadata.yaml").read_text(encoding="utf-8")).group(1).lstrip("v")
for where, ver in (("main", ver_main), ("metadata", ver_meta)):
    check(f"version {where}=={ver_readme}", ver == ver_readme, f"{ver} vs {ver_readme}")
soak_first = (ROOT / "SOAK.md").read_text(encoding="utf-8").splitlines()[0]
check("SOAK version", f"v{ver_readme}" in soak_first, soak_first)

# 7. arch table files
for fname in ("client.py", "throttle.py", "reference.py", "pipeline.py", "ingest.py",
              "store.py", "tool.py", "query.py", "runlog.py"):
    check(f"arch file {fname}", (ROOT / "bili" / fname).is_file())

# 8. test count
m = re.search(r"(\d+) 个测试", README)
if m:
    out = subprocess.run(
        [sys.executable, "tests/test_core.py"], capture_output=True, text=True, cwd=str(ROOT)
    ).stderr
    found = re.search(r"Ran (\d+) tests", out)
    check("test count matches",
          found and int(found.group(1)) == int(m.group(1)),
          f"README={m.group(1)} actual={found.group(1) if found else '?'}")

print()
if issues:
    print(f"{len(issues)} MISMATCHES")
    sys.exit(1)
print("ALL CHECKS PASSED")

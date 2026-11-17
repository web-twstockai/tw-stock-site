"""把 build-info.json 的 source 標成 github-actions（原本的描述移到 source_detail），並加入 run 資訊。
用法：python tools/cloud/stamp_build_info.py <匯出目錄>"""
import json, os, subprocess, sys
from pathlib import Path

p = Path(sys.argv[1]) / "build-info.json"
b = json.loads(p.read_text("utf-8"))
b["source_detail"] = b.get("source")
b["source"] = "github-actions"
repo, rid = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", "")
b["github_actions"] = {"run_id": rid, "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"), "workflow": os.environ.get("GITHUB_WORKFLOW"),
                       "job": os.environ.get("TWSITE_CLOUD_JOB"), "code_sha": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None,
                       "workflow_sha": os.environ.get("GITHUB_SHA"),
                       "run_url": f"https://github.com/{repo}/actions/runs/{rid}" if rid else None}
p.write_text(json.dumps(b, ensure_ascii=False, indent=1), "utf-8")
print("build-info.json source =", b["source"])

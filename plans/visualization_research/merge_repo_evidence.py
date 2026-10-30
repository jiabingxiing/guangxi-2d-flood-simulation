"""Merge the original verifier reports without replacing API license fields."""
import json
from pathlib import Path

plans = Path(__file__).resolve().parents[1]
reports = ["repo-maplibre.json", "repo-geotiffjs.json", "repo-georaster.json", "repo-gdal.json", "repo-pmtiles.json", "repo-tippecanoe.json", "visualization_research/repo-proj4js.json"]
rows = []
for filename in reports:
    payload = json.loads((plans / filename).read_text(encoding="utf-8"))
    for item in payload["repos"]:
        assert item["status"] in {"verified", "verified-web"}
        rows.append({**item, "source_report": filename})
result = {"fetched_at": max(x["fetched_at"] for x in rows), "method": "merge of seven original verify_repos.py reports; API fields preserved", "repos": rows}
(plans / "flood-visualization-repos.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
lines = ["# 开源仓库核验汇总", "", "六仓库 API 核验通过，Proj4js 网页降级核验通过。此表替代首次网络失败表。API 的 NOASSERTION/null 保留原值，不替代人工许可审查。", "", "| 仓库 | 状态 | Stars | API 许可证 | 最近推送 UTC | archived | 原始记录 |", "|---|---|---:|---|---|---|---|"]
for x in rows:
    lines.append(f"| [{x['repo']}](<{x['url']}>) | {x['status']} | {x['stars']} | {x['license']} | {x['pushed_at']} | {x['archived']} | [{x['source_report']}](<plans/{x['source_report']}>) |")
(plans / "flood-visualization-repos.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
print("merged verified repositories:", len(rows))

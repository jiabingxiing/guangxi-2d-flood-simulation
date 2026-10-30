"""floodviz 命令行入口：inventory / prepare / validate。

python -m floodviz inventory --config configs/default.yaml
python -m floodviz prepare   --config configs/default.yaml --bundle-id v3-20261028
python -m floodviz validate  --bundle outputs/web_bundle/v3-20261028 --full
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .quality.frames import audit_run

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "configs" / "default.yaml"


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_inventory(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(f"[inventory] 项目={config.project.get('title')} 案例={len(config.cases)} 父项目={config.parent_root}")
    summary = {"ok": True, "cases": {}}
    for case_id, case in config.cases.items():
        for run in case.runs.values():
            saveint = run.save_interval_s
            audit = audit_run(
                run.source_dir,
                save_interval_s=saveint,
                simulation_duration_s=run.simulation_duration_s,
                expected_count=run.expected_frame_count,
                compute_sha256=False,
            )
            header = audit.frames[0].header if audit.frames else {}
            status = "OK" if audit.ok else "REJECT"
            print(
                f"  [{status}] {case_id}/{run.run_id}: frames={audit.count}/{audit.expected_count} "
                f"grid={int(header.get('ncols', 0))}x{int(header.get('nrows', 0))} "
                f"cell={header.get('cellsize')}m saveint={saveint:.0f}s"
            )
            for reason in audit.reject_reasons:
                print(f"      ! {reason}")
            for warning in audit.warnings:
                print(f"      ~ {warning}")
            summary["cases"].setdefault(case_id, {})[run.run_id] = audit.to_dict()
            if not audit.ok:
                summary["ok"] = False
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[inventory] 明细：{args.json_out}")
    print(f"[inventory] 总体：{'通过' if summary['ok'] else '存在拒绝项（不得发布）'}")
    return 0 if summary["ok"] else 2


def cmd_prepare(args: argparse.Namespace) -> int:
    from .pipeline import PipelineError, prepare_bundle

    config = load_config(args.config)
    try:
        bundle = prepare_bundle(
            config,
            args.bundle_id,
            only=tuple(args.only) if args.only else None,
            frames_limit=args.frames_limit,
            skip_tiles=args.skip_tiles,
        )
    except PipelineError as exc:
        print(f"[prepare] 失败：{exc}", file=sys.stderr)
        return 1
    print(f"[prepare] 发布包：{bundle}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .pipeline import validate_bundle

    config = None
    if args.config and Path(args.config).is_file():
        config = load_config(args.config)
    report = validate_bundle(Path(args.bundle), config, full=args.full)
    ok = bool(getattr(report, "ok", False))
    as_dict = getattr(report, "to_dict", None)
    payload = as_dict() if callable(as_dict) else report if isinstance(report, dict) else {"ok": ok}
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[validate] 报告：{args.json_out}")
    else:
        _print(payload)
    print(f"[validate] 结果：{'通过' if ok else '未通过'}")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="floodviz", description="二维洪涝可视化发布工程")
    parser.add_argument("--version", action="version", version="floodviz 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="扫描源 run、校验帧序与栅格头")
    inv.add_argument("--config", default=str(DEFAULT_CONFIG))
    inv.add_argument("--json-out", default=None)
    inv.set_defaults(func=cmd_inventory)

    prep = sub.add_parser("prepare", help="生成发布包")
    prep.add_argument("--config", default=str(DEFAULT_CONFIG))
    prep.add_argument("--bundle-id", required=True)
    prep.add_argument("--only", nargs="*", default=None, help="只构建指定案例（调试用）")
    prep.add_argument("--frames-limit", type=int, default=None, help="每 run 只导出前 N 帧（调试用）")
    prep.add_argument("--skip-tiles", action="store_true", help="跳过显示图（调试用）")
    prep.set_defaults(func=cmd_prepare)

    val = sub.add_parser("validate", help="校验发布包")
    val.add_argument("--bundle", required=True)
    val.add_argument("--config", default=str(DEFAULT_CONFIG))
    val.add_argument("--full", action="store_true", help="全量数值/统计/空间校验")
    val.add_argument("--json-out", default=None)
    val.set_defaults(func=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"[floodviz] 配置错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

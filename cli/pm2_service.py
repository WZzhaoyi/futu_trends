"""PM2 process management for long-running project services.

This module intentionally uses only the Python standard library so process
management remains available even when market-data dependencies are absent.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANAGED_ACTIONS = ("start", "restart", "stop", "delete", "logs", "status")


class PM2ConfigError(ValueError):
    """Invalid local PM2 command configuration."""


@dataclass(frozen=True)
class ServiceSpec:
    service: str
    config_path: Path
    instance_name: str
    ecosystem_path: Path
    environment: dict[str, str]


def _resolved_config(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    # 对齐 Node path.resolve：绝对化并规范化，但不解引用符号链接。
    path = Path(os.path.abspath(path))
    if not path.is_file():
        raise PM2ConfigError(f"配置文件不存在: {path}")
    return path


def _config_slug(path: Path) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", path.stem).strip("-")
    return slug or "config"


def _resolved_runtime_dir(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise PM2ConfigError("--runtime-dir 必须使用绝对路径")
    return Path(os.path.abspath(path))


def _resolved_project_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return Path(os.path.abspath(path))


def _identity_path(path: Path, platform: str) -> str:
    identity = str(path)
    return identity.lower() if platform == "win32" else identity


def build_service_spec(
    service: str,
    config: str,
    *,
    port: int = 8001,
    runtime_dir: str | None = None,
    symbol: str = "SH.000902",
    initial_position: str = "flat",
    entry_date: str | None = None,
    notification_mode: str = "position-aware",
    window_months: int = 9,
    t1_sell_mode: str = "defer-next-open",
    strategy_file: str | None = None,
    cache_dir: str | None = None,
    interval: float = 60.0,
    nav_refresh: float = 900.0,
    max_nav_age: int = 14,
    max_quote_age: float = 180.0,
    max_errors: int = 5,
    platform: str = sys.platform,
) -> ServiceSpec:
    """Resolve one PM2 instance exactly as its ecosystem file does."""
    config_path = _resolved_config(config)
    identity = _identity_path(config_path, platform)
    slug = _config_slug(config_path)

    if service == "order-engine":
        digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
        return ServiceSpec(
            service=service,
            config_path=config_path,
            instance_name=f"futu-order-{slug}-{digest}",
            ecosystem_path=PROJECT_ROOT / "order_engine" / "ecosystem.order-engine.config.js",
            environment={"ORDER_ENGINE_CONFIG": str(config_path)},
        )

    if service == "signal-api":
        if not 1 <= port <= 65535:
            raise PM2ConfigError(f"端口超出范围 1-65535: {port}")
        digest = hashlib.sha256(f"{identity}\0{port}".encode()).hexdigest()[:8]
        return ServiceSpec(
            service=service,
            config_path=config_path,
            instance_name=f"futu-signal-api-{slug}-{port}-{digest}",
            ecosystem_path=PROJECT_ROOT / "gui" / "backend" / "ecosystem.signal-api.config.js",
            environment={
                "SIGNAL_API_CONFIG": str(config_path),
                "SIGNAL_API_PORT": str(port),
            },
        )

    if service == "csi-flow":
        if not runtime_dir:
            raise PM2ConfigError("csi-flow 必须指定 --runtime-dir")
        runtime_path = _resolved_runtime_dir(runtime_dir)
        if initial_position not in {"flat", "long"}:
            raise PM2ConfigError("initial-position 只能是 flat 或 long")
        if initial_position == "long" and not entry_date:
            raise PM2ConfigError(
                "initial-position=long 必须同时指定 --entry-date"
            )
        if notification_mode not in {"position-aware", "position-independent"}:
            raise PM2ConfigError("notification-mode 无效")
        if window_months < 1:
            raise PM2ConfigError("window-months 必须不小于 1")
        if t1_sell_mode not in {"defer-next-open", "ignore-same-day"}:
            raise PM2ConfigError("t1-sell-mode 无效")
        digest_input = "\0".join(
            (
                identity,
                _identity_path(runtime_path, platform),
                symbol.upper(),
            )
        )
        digest = hashlib.sha256(digest_input.encode()).hexdigest()[:8]
        environment = {
            "CSI_FLOW_CONFIG": str(config_path),
            "CSI_FLOW_RUNTIME_DIR": str(runtime_path),
            "CSI_FLOW_SYMBOL": symbol.upper(),
            "CSI_FLOW_INITIAL_POSITION": initial_position,
            "CSI_FLOW_NOTIFICATION_MODE": notification_mode,
            "CSI_FLOW_WINDOW_MONTHS": str(window_months),
            "CSI_FLOW_T1_SELL_MODE": t1_sell_mode,
        }
        if entry_date:
            environment["CSI_FLOW_ENTRY_DATE"] = entry_date
        return ServiceSpec(
            service=service,
            config_path=config_path,
            instance_name=f"futu-csi-flow-{digest}",
            ecosystem_path=(
                PROJECT_ROOT
                / "market_analysis"
                / "ecosystem.csi-flow.config.js"
            ),
            environment=environment,
        )

    if service == "etf-premium":
        if not runtime_dir:
            raise PM2ConfigError("etf-premium 必须指定 --runtime-dir")
        runtime_path = _resolved_runtime_dir(runtime_dir)
        if not symbol.isdigit() or len(symbol) != 6:
            raise PM2ConfigError("symbol 必须是6位基金代码")
        if initial_position not in {"base", "low"}:
            raise PM2ConfigError("initial-position 只能是 base 或 low")
        if min(interval, nav_refresh, max_quote_age) <= 0:
            raise PM2ConfigError(
                "interval、nav-refresh 和 max-quote-age 必须大于0"
            )
        if min(max_nav_age, max_errors) <= 0:
            raise PM2ConfigError("max-nav-age 和 max-errors 必须是正整数")
        digest_input = "\0".join(
            (
                identity,
                _identity_path(runtime_path, platform),
                symbol,
            )
        )
        digest = hashlib.sha256(digest_input.encode()).hexdigest()[:8]
        environment = {
            "ETF_PREMIUM_CONFIG": str(config_path),
            "ETF_PREMIUM_RUNTIME_DIR": str(runtime_path),
            "ETF_PREMIUM_SYMBOL": symbol,
            "ETF_PREMIUM_INITIAL_POSITION": initial_position,
            "ETF_PREMIUM_INTERVAL": str(interval),
            "ETF_PREMIUM_NAV_REFRESH": str(nav_refresh),
            "ETF_PREMIUM_MAX_NAV_AGE": str(max_nav_age),
            "ETF_PREMIUM_MAX_QUOTE_AGE": str(max_quote_age),
            "ETF_PREMIUM_MAX_ERRORS": str(max_errors),
        }
        if strategy_file:
            strategy_path = _resolved_project_path(strategy_file)
            if not strategy_path.is_file():
                raise PM2ConfigError(f"策略文件不存在: {strategy_path}")
            environment["ETF_PREMIUM_STRATEGY_FILE"] = str(strategy_path)
        if cache_dir:
            environment["ETF_PREMIUM_CACHE_DIR"] = str(
                _resolved_project_path(cache_dir)
            )
        return ServiceSpec(
            service=service,
            config_path=config_path,
            instance_name=f"futu-etf-premium-{digest}",
            ecosystem_path=(
                PROJECT_ROOT
                / "market_analysis"
                / "ecosystem.etf-premium.config.js"
            ),
            environment=environment,
        )

    raise PM2ConfigError(f"不支持的 PM2 服务: {service}")


def build_pm2_args(action: str, spec: ServiceSpec) -> list[str]:
    if action not in MANAGED_ACTIONS:
        raise PM2ConfigError(f"不支持的 PM2 操作: {action}")
    if action == "start":
        return [
            "start", str(spec.ecosystem_path),
            "--only", spec.instance_name,
            "--update-env",
        ]
    if action == "restart":
        return ["restart", spec.instance_name, "--update-env"]
    if action == "logs":
        return ["logs", spec.instance_name, "--lines", "100"]
    if action == "status":
        return ["describe", spec.instance_name]
    return [action, spec.instance_name]


def resolve_pm2_invocation(
    args: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    platform: str = sys.platform,
) -> list[str]:
    env = os.environ if environ is None else environ
    explicit = env.get("PM2_BIN")
    if platform != "win32":
        return [explicit or "pm2", *args]

    if explicit and not explicit.lower().endswith(".cmd"):
        return [explicit, *args]

    command_file = explicit or shutil.which("pm2.cmd")
    if command_file:
        pm2_cli = Path(command_file).parent / "node_modules" / "pm2" / "bin" / "pm2"
        if pm2_cli.is_file():
            return [sys.executable, str(pm2_cli), *args]
    return ["pm2.exe", *args]


def run_pm2(args: Sequence[str], extra_env: Mapping[str, str] | None = None) -> int:
    command = resolve_pm2_invocation(args)
    env = {**os.environ, **(extra_env or {})}
    try:
        return subprocess.run(command, cwd=PROJECT_ROOT, env=env).returncode
    except FileNotFoundError:
        print("未找到 PM2。请先执行 npm install --global pm2", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"PM2 执行失败: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="futu-trends pm2",
        description=(
            "管理 order-engine、signal-api、csi-flow 与 etf-premium 的 PM2 进程"
        ),
    )
    targets = parser.add_subparsers(dest="target", required=True)

    for service, help_text in (
        ("order-engine", "管理条件单引擎"),
        ("signal-api", "管理 Signal API"),
    ):
        child = targets.add_parser(service, help=help_text)
        child.add_argument("action", choices=MANAGED_ACTIONS)
        child.add_argument("--config", required=True, help="配置文件路径（必填，无默认）")
        if service == "signal-api":
            child.add_argument("--port", type=int, default=8001, help="API 端口（默认 8001）")

    csi_flow = targets.add_parser(
        "csi-flow",
        help="管理中证流通 M1 长期信号服务",
    )
    csi_flow.add_argument("action", choices=MANAGED_ACTIONS)
    csi_flow.add_argument(
        "--config",
        required=True,
        help="项目配置文件绝对或项目相对路径",
    )
    csi_flow.add_argument(
        "--runtime-dir",
        required=True,
        help="live 运行目录绝对路径",
    )
    csi_flow.add_argument("--symbol", default="SH.000902")
    csi_flow.add_argument(
        "--initial-position",
        choices=("flat", "long"),
        default="flat",
    )
    csi_flow.add_argument("--entry-date")
    csi_flow.add_argument(
        "--notification-mode",
        choices=("position-aware", "position-independent"),
        default="position-aware",
    )
    csi_flow.add_argument("--window-months", type=int, default=9)
    csi_flow.add_argument(
        "--t1-sell-mode",
        choices=("defer-next-open", "ignore-same-day"),
        default="defer-next-open",
    )

    etf_premium = targets.add_parser(
        "etf-premium",
        help="管理 ETF T-1 折溢价长期通知服务",
    )
    etf_premium.add_argument("action", choices=MANAGED_ACTIONS)
    etf_premium.add_argument("--config", required=True)
    etf_premium.add_argument("--runtime-dir", required=True)
    etf_premium.add_argument("--symbol", default="159941")
    etf_premium.add_argument("--strategy-file")
    etf_premium.add_argument("--cache-dir")
    etf_premium.add_argument(
        "--initial-position",
        choices=("base", "low"),
        default="base",
    )
    etf_premium.add_argument("--interval", type=float, default=60.0)
    etf_premium.add_argument("--nav-refresh", type=float, default=900.0)
    etf_premium.add_argument("--max-nav-age", type=int, default=14)
    etf_premium.add_argument("--max-quote-age", type=float, default=180.0)
    etf_premium.add_argument("--max-errors", type=int, default=5)

    targets.add_parser("save", help="保存当前 PM2 进程列表")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.target == "save":
        return run_pm2(["save"])
    try:
        spec = build_service_spec(
            args.target,
            args.config,
            port=getattr(args, "port", 8001),
            runtime_dir=getattr(args, "runtime_dir", None),
            symbol=getattr(args, "symbol", "SH.000902"),
            initial_position=getattr(args, "initial_position", "flat"),
            entry_date=getattr(args, "entry_date", None),
            notification_mode=getattr(
                args, "notification_mode", "position-aware"
            ),
            window_months=getattr(args, "window_months", 9),
            t1_sell_mode=getattr(
                args, "t1_sell_mode", "defer-next-open"
            ),
            strategy_file=getattr(args, "strategy_file", None),
            cache_dir=getattr(args, "cache_dir", None),
            interval=getattr(args, "interval", 60.0),
            nav_refresh=getattr(args, "nav_refresh", 900.0),
            max_nav_age=getattr(args, "max_nav_age", 14),
            max_quote_age=getattr(args, "max_quote_age", 180.0),
            max_errors=getattr(args, "max_errors", 5),
        )
        pm2_args = build_pm2_args(args.action, spec)
    except PM2ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return run_pm2(pm2_args, spec.environment)


if __name__ == "__main__":
    raise SystemExit(main())

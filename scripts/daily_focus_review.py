from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dependency fallback for very small envs
    load_dotenv = None

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


PROJECT_DIR = Path(__file__).resolve().parents[1]
WATCHLIST_FILE = PROJECT_DIR / "watchlist_stocks.csv"
REPORTS_DIR = PROJECT_DIR / "reports"
LOGS_DIR = PROJECT_DIR / "logs"
DEFAULT_YUNAI_BASE_URL = "https://quant.yunai.com.cn/quant-market"
CN_TZ = ZoneInfo("Asia/Shanghai") if ZoneInfo is not None else timezone.utc
SECRET_KEYS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "WEBHOOK",
    "AUTHORIZATION",
)


@dataclass
class WatchStock:
    theme: str
    name: str
    code: str


@dataclass
class QuoteRow:
    item: WatchStock
    price: float | None = None
    change_pct: float | None = None
    volume_ratio: float | None = None
    turnover_rate: float | None = None
    amount: float | None = None
    latest_time: datetime | None = None
    raw_name: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.raw_name or self.item.name


@dataclass
class ThemeStat:
    theme: str
    count: int = 0
    valid_count: int = 0
    up_count: int = 0
    avg_change: float = 0.0
    score: float = 0.0
    leader: QuoteRow | None = None


def print_status(message: str) -> None:
    print(message, flush=True)


def load_project_env() -> None:
    env_path = PROJECT_DIR / ".env"
    if load_dotenv is not None:
        load_dotenv(env_path)
        return
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int((os.getenv(name) or "").strip() or default)
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float((os.getenv(name) or "").strip() or default)
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def redact(value: str) -> str:
    redacted = value
    for key, secret in os.environ.items():
        upper = key.upper()
        if not any(marker in upper for marker in SECRET_KEYS):
            continue
        secret = secret.strip()
        if len(secret) >= 8:
            redacted = redacted.replace(secret, "[已隐藏]")
    return redacted


def split_stock_list(value: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in value.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",").split(","):
        code = raw.strip()
        if not code:
            continue
        key = code.upper()
        if key in seen:
            continue
        seen.add(key)
        result.append(code)
    return result


def load_watchlist() -> list[WatchStock]:
    items: list[WatchStock] = []
    if WATCHLIST_FILE.exists():
        with WATCHLIST_FILE.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                code = (row.get("code") or "").strip()
                if not code:
                    continue
                items.append(
                    WatchStock(
                        theme=(row.get("theme") or "自选股").strip() or "自选股",
                        name=(row.get("name") or code).strip() or code,
                        code=code,
                    )
                )
    if items:
        return items

    codes = split_stock_list(os.getenv("STOCK_LIST") or os.getenv("STOCK_LIST_CONFIG") or "600519")
    return [WatchStock(theme="自选股", name=code, code=code) for code in codes]


def normalize_symbol(code: str) -> str:
    value = code.strip().upper()
    if value.startswith("HK") and value[2:].isdigit():
        return value[2:].zfill(5)
    if value.endswith(".HK"):
        return value[:-3].zfill(5)
    if value.endswith((".SH", ".SZ", ".BJ")):
        return value.split(".", 1)[0]
    return value


def clean_base_url(value: str | None) -> str:
    base = (value or DEFAULT_YUNAI_BASE_URL).strip().rstrip("/")
    marker = "/api/v1/"
    if marker in base:
        base = base.split(marker, 1)[0].rstrip("/")
    return base or DEFAULT_YUNAI_BASE_URL


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def ratio_to_percent(value: Any) -> float | None:
    number = safe_float(value)
    if number is None:
        return None
    if abs(number) <= 1:
        number *= 100
    return round(number, 2)


def timestamp_ms_to_datetime(value: Any) -> datetime | None:
    number = safe_float(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, tz=timezone.utc).astimezone(CN_TZ)
    except (OSError, OverflowError, ValueError):
        return None


def yunai_headers() -> dict[str, str]:
    authorization = (
        os.getenv("YUNAI_AUTHORIZATION")
        or os.getenv("YUNAI_API_KEY")
        or ""
    ).strip()
    if not authorization:
        raise RuntimeError("YunAI 未配置：缺少 YUNAI_AUTHORIZATION 或 YUNAI_API_KEY")
    if not authorization.lower().startswith("bearer "):
        authorization = f"Bearer {authorization}"
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Authorization": authorization,
    }


def fetch_yunai_quotes(watchlist: list[WatchStock], batch_size: int) -> dict[str, dict[str, Any]]:
    base_url = clean_base_url(os.getenv("YUNAI_BASE_URL"))
    url = f"{base_url}/api/v1/quantitative/quotes/real-time-quotes"
    symbol_to_code = {normalize_symbol(item.code): item.code for item in watchlist}
    symbols = list(symbol_to_code)
    all_quotes: dict[str, dict[str, Any]] = {}

    for start in range(0, len(symbols), batch_size):
        chunk = symbols[start : start + batch_size]
        response = requests.post(
            url,
            json={"symbols": chunk},
            headers=yunai_headers(),
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            continue
        for symbol in chunk:
            quote = payload.get(symbol)
            code = symbol_to_code[symbol]
            if isinstance(quote, dict):
                all_quotes[code] = quote
        print_status(f"已获取行情：{min(start + len(chunk), len(symbols))}/{len(symbols)}")
    return all_quotes


def build_quote_rows(watchlist: list[WatchStock], raw_quotes: dict[str, dict[str, Any]]) -> list[QuoteRow]:
    rows: list[QuoteRow] = []
    for item in watchlist:
        quote = raw_quotes.get(item.code) or raw_quotes.get(normalize_symbol(item.code)) or {}
        row = QuoteRow(
            item=item,
            price=safe_float(quote.get("latestPrice") or quote.get("close")),
            change_pct=ratio_to_percent(quote.get("changeRate")),
            volume_ratio=safe_float(quote.get("volumeRatio")),
            turnover_rate=ratio_to_percent(quote.get("turnoverRate")),
            amount=safe_float(quote.get("amount")),
            latest_time=timestamp_ms_to_datetime(quote.get("latestTime")),
            raw_name=str(quote.get("name") or "").strip(),
        )
        rows.append(row)
    return rows


def score_themes(rows: list[QuoteRow]) -> dict[str, ThemeStat]:
    stats: dict[str, ThemeStat] = {}
    grouped: dict[str, list[QuoteRow]] = {}
    for row in rows:
        grouped.setdefault(row.item.theme, []).append(row)

    for theme, theme_rows in grouped.items():
        valid = [row for row in theme_rows if row.change_pct is not None]
        stat = ThemeStat(theme=theme, count=len(theme_rows), valid_count=len(valid))
        if valid:
            stat.up_count = sum(1 for row in valid if (row.change_pct or 0) > 0)
            stat.avg_change = round(sum(row.change_pct or 0 for row in valid) / len(valid), 2)
            stat.leader = max(valid, key=lambda row: row.change_pct or -999)
            up_ratio = stat.up_count / len(valid)
            stat.score = round(stat.avg_change * 1.2 + up_ratio * 2.0, 2)
        stats[theme] = stat
    return stats


def score_rows(
    rows: list[QuoteRow],
    theme_stats: dict[str, ThemeStat],
    *,
    min_change_pct: float,
    min_abs_change_pct: float,
    min_volume_ratio: float,
    top_themes: int,
) -> list[QuoteRow]:
    hot_themes = {
        stat.theme
        for stat in sorted(theme_stats.values(), key=lambda item: item.score, reverse=True)[:top_themes]
        if stat.valid_count > 0 and stat.score > 0
    }

    for row in rows:
        change = row.change_pct
        volume_ratio = row.volume_ratio
        turnover = row.turnover_rate
        theme_score = theme_stats.get(row.item.theme, ThemeStat(row.item.theme)).score
        reasons: list[str] = []
        score = 0.0

        if change is not None:
            if change >= min_change_pct:
                reasons.append(f"涨幅 {change:.2f}%")
            if abs(change) >= min_abs_change_pct:
                reasons.append(f"异动 {change:.2f}%")
            score += max(change, 0) * 2.0
            score += max(abs(change) - min_abs_change_pct, 0) * 0.8
            if change < -min_abs_change_pct:
                score += abs(change) * 0.7
        if volume_ratio is not None:
            if volume_ratio >= min_volume_ratio:
                reasons.append(f"量比 {volume_ratio:.2f}")
            score += min(max(volume_ratio, 0), 5) * 1.4
        if turnover is not None:
            if turnover >= 3:
                reasons.append(f"换手 {turnover:.2f}%")
            score += min(max(turnover, 0), 12) * 0.25
        if row.item.theme in hot_themes:
            reasons.append("所属板块靠前")
            score += max(theme_score, 0) * 1.5
        if row.amount is not None and row.amount >= 1_000_000_000:
            reasons.append("成交额较大")
            score += min(math.log10(row.amount) - 8, 3)

        row.score = round(score, 2)
        row.reasons = reasons or ["观察"]

    candidates = [
        row
        for row in rows
        if row.change_pct is not None
        and (
            row.change_pct >= min_change_pct
            or abs(row.change_pct) >= min_abs_change_pct
            or (row.volume_ratio is not None and row.volume_ratio >= min_volume_ratio)
            or row.item.theme in hot_themes
        )
    ]
    return sorted(candidates, key=lambda row: row.score, reverse=True)


def select_focus_rows(
    candidates: list[QuoteRow],
    theme_stats: dict[str, ThemeStat],
    max_stocks: int,
    top_themes: int,
) -> list[QuoteRow]:
    selected: list[QuoteRow] = []
    selected_codes: set[str] = set()
    hot_theme_names = [
        stat.theme
        for stat in sorted(theme_stats.values(), key=lambda item: item.score, reverse=True)[:top_themes]
        if stat.valid_count > 0 and stat.score > 0
    ]

    for theme in hot_theme_names:
        theme_candidates = [row for row in candidates if row.item.theme == theme]
        if not theme_candidates:
            continue
        leader = max(theme_candidates, key=lambda row: row.score)
        if leader.item.code not in selected_codes:
            selected.append(leader)
            selected_codes.add(leader.item.code)
        if len(selected) >= max_stocks:
            return selected

    for row in candidates:
        if row.item.code in selected_codes:
            continue
        selected.append(row)
        selected_codes.add(row.item.code)
        if len(selected) >= max_stocks:
            break
    return selected


def is_fresh_market_data(rows: list[QuoteRow]) -> bool | None:
    dated = [row.latest_time.date() for row in rows if row.latest_time is not None]
    if not dated:
        return None
    today = datetime.now(CN_TZ).date()
    fresh_count = sum(1 for item in dated if item == today)
    return fresh_count >= max(3, len(dated) // 3)


def fmt_number(value: float | None, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}{suffix}"


def fmt_amount(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.0f}万"
    return f"{value:.0f}"


def build_report(
    rows: list[QuoteRow],
    candidates: list[QuoteRow],
    selected: list[QuoteRow],
    theme_stats: dict[str, ThemeStat],
    top_themes: int,
    stale_warning: str | None,
) -> str:
    now = datetime.now(CN_TZ)
    valid_count = sum(1 for row in rows if row.change_pct is not None)
    up_count = sum(1 for row in rows if row.change_pct is not None and row.change_pct > 0)
    down_count = sum(1 for row in rows if row.change_pct is not None and row.change_pct < 0)
    hot_themes = sorted(theme_stats.values(), key=lambda item: item.score, reverse=True)[:top_themes]

    lines: list[str] = [
        f"# DailyStock 今日重点筛选 {now:%Y-%m-%d}",
        "",
        f"- 自选股：{len(rows)} 只，取得有效行情：{valid_count} 只",
        f"- 上涨/下跌：{up_count}/{down_count}",
        f"- 入选重点观察：{len(selected)} 只",
        "- 筛选逻辑：先看自选股的涨跌幅、量比、换手和成交额，再用板块热度补足覆盖面；AI 只留给入选个股。",
    ]
    if stale_warning:
        lines.append(f"- 注意：{stale_warning}")

    lines.extend(["", "## 热点板块 Top"])
    lines.append("|板块|均涨幅|上涨数|龙头|分数|")
    lines.append("|---|---:|---:|---|---:|")
    for stat in hot_themes:
        leader = "-"
        if stat.leader is not None:
            leader = f"{stat.leader.name}({stat.leader.item.code}) {fmt_number(stat.leader.change_pct, '%')}"
        lines.append(
            f"|{stat.theme}|{fmt_number(stat.avg_change, '%')}|{stat.up_count}/{stat.valid_count or stat.count}|{leader}|{fmt_number(stat.score, '')}|"
        )

    lines.extend(["", "## 今日重点看的票"])
    if selected:
        lines.append("|代码|名称|板块|涨跌幅|量比|换手|成交额|原因|")
        lines.append("|---|---|---|---:|---:|---:|---:|---|")
        for row in selected:
            lines.append(
                "|{code}|{name}|{theme}|{chg}|{vr}|{turn}|{amount}|{reason}|".format(
                    code=row.item.code,
                    name=row.name,
                    theme=row.item.theme,
                    chg=fmt_number(row.change_pct, "%"),
                    vr=fmt_number(row.volume_ratio, ""),
                    turn=fmt_number(row.turnover_rate, "%"),
                    amount=fmt_amount(row.amount),
                    reason="、".join(row.reasons[:4]),
                )
            )
    else:
        lines.append("今天没有筛出足够强的重点个股，建议只看大盘和板块日报。")

    lines.extend(["", "## 备选观察"])
    backup = [row for row in candidates if row.item.code not in {item.item.code for item in selected}][:10]
    if backup:
        lines.append("|代码|名称|板块|涨跌幅|量比|原因|")
        lines.append("|---|---|---|---:|---:|---|")
        for row in backup:
            lines.append(
                f"|{row.item.code}|{row.name}|{row.item.theme}|{fmt_number(row.change_pct, '%')}|{fmt_number(row.volume_ratio, '')}|{'、'.join(row.reasons[:3])}|"
            )
    else:
        lines.append("无。")

    return "\n".join(lines) + "\n"


def save_report(content: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"focus_screen_{datetime.now(CN_TZ):%Y%m%d}.md"
    path.write_text(content, encoding="utf-8")
    return path


def ding_url_with_sign(webhook: str, secret: str) -> str:
    if not secret:
        return webhook
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(digest))
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={sign}"


def build_dingtalk_markdown(selected: list[QuoteRow], theme_stats: dict[str, ThemeStat], top_themes: int, report_path: Path) -> str:
    now = datetime.now(CN_TZ)
    hot_themes = sorted(theme_stats.values(), key=lambda item: item.score, reverse=True)[:top_themes]
    lines = [
        f"### DailyStock 今日重点筛选 {now:%Y-%m-%d}",
        "",
        f"- 入选重点观察：{len(selected)} 只",
        f"- 本地报告：{report_path.name}",
        "",
        "**热点板块**",
    ]
    for stat in hot_themes[:5]:
        leader = ""
        if stat.leader is not None:
            leader = f"，龙头 {stat.leader.name} {fmt_number(stat.leader.change_pct, '%')}"
        lines.append(f"- {stat.theme}：均涨幅 {fmt_number(stat.avg_change, '%')}，上涨 {stat.up_count}/{stat.valid_count or stat.count}{leader}")

    lines.extend(["", "**重点个股**"])
    if not selected:
        lines.append("- 暂无强信号，今天以大盘和板块复盘为主。")
    for row in selected:
        lines.append(
            f"- {row.name}({row.item.code}) {fmt_number(row.change_pct, '%')}，{row.item.theme}，{'、'.join(row.reasons[:3])}"
        )
    return "\n".join(lines)


def send_dingtalk(markdown: str) -> bool:
    webhook = (os.getenv("DINGTALK_WEBHOOK_URL") or "").strip()
    if not webhook:
        print_status("钉钉未配置：跳过推送。")
        return False
    secret = (os.getenv("DINGTALK_SECRET") or "").strip()
    url = ding_url_with_sign(webhook, secret)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "DailyStock 今日重点筛选",
            "text": markdown,
        },
        "at": {"isAtAll": False},
    }
    response = requests.post(url, json=payload, timeout=15)
    try:
        data = response.json()
    except Exception:
        data = {}
    if response.ok and data.get("errcode", 0) == 0:
        print_status("钉钉重点筛选摘要已发送。")
        return True
    message = data.get("errmsg") if isinstance(data, dict) else response.text
    raise RuntimeError(f"钉钉推送失败：{message or response.status_code}")


def run_subprocess(args: list[str]) -> int:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    command = [sys.executable] + args
    print_status("执行：" + redact(" ".join(command)))
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line.rstrip(), flush=True)
    return process.wait()


def write_run_log(summary: dict[str, Any]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"focus_screen_{datetime.now(CN_TZ):%Y%m%d}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="收盘后快速筛选今日重点自选股。")
    parser.add_argument("--push", action="store_true", help="发送钉钉重点筛选摘要")
    parser.add_argument("--run-market", action="store_true", help="筛选后运行大盘复盘")
    parser.add_argument("--run-ai", action="store_true", help="筛选后只对入选个股运行 AI 分析")
    parser.add_argument("--force-run", action="store_true", help="传给 main.py，跳过交易日检查")
    parser.add_argument("--screen-only", action="store_true", help="仅生成筛选报告，不推送、不运行 AI")
    parser.add_argument("--allow-stale", action="store_true", help="允许行情日期不是今天时仍继续推送/AI")
    parser.add_argument("--max-stocks", type=int, default=None, help="最多入选个股数，默认读取 FOCUS_MAX_STOCKS")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_project_env()

    max_stocks = args.max_stocks or env_int("FOCUS_MAX_STOCKS", 8, minimum=1)
    top_themes = env_int("FOCUS_TOP_THEMES", 5, minimum=1)
    batch_size = env_int("FOCUS_BATCH_SIZE", 50, minimum=1)
    min_change_pct = env_float("FOCUS_MIN_CHANGE_PCT", 2.0, minimum=0)
    min_abs_change_pct = env_float("FOCUS_MIN_ABS_CHANGE_PCT", 3.0, minimum=0)
    min_volume_ratio = env_float("FOCUS_MIN_VOLUME_RATIO", 1.5, minimum=0)
    require_fresh = env_bool("FOCUS_REQUIRE_FRESH", True)

    watchlist = load_watchlist()
    if not watchlist:
        raise RuntimeError("自选股为空：请先配置 STOCK_LIST 或 watchlist_stocks.csv")

    print_status(f"开始快速筛选：自选股 {len(watchlist)} 只，最多入选 {max_stocks} 只。")
    raw_quotes = fetch_yunai_quotes(watchlist, batch_size=batch_size)
    rows = build_quote_rows(watchlist, raw_quotes)
    theme_stats = score_themes(rows)
    candidates = score_rows(
        rows,
        theme_stats,
        min_change_pct=min_change_pct,
        min_abs_change_pct=min_abs_change_pct,
        min_volume_ratio=min_volume_ratio,
        top_themes=top_themes,
    )
    selected = select_focus_rows(candidates, theme_stats, max_stocks=max_stocks, top_themes=top_themes)

    fresh = is_fresh_market_data(rows)
    stale_warning = None
    if fresh is False:
        stale_warning = "行情日期不是今天，可能是非交易日或数据尚未更新；默认不推送、不运行 AI。"
        print_status(stale_warning)
    elif fresh is None:
        print_status("行情未返回明确日期，继续按当前行情生成报告。")

    report = build_report(
        rows,
        candidates,
        selected,
        theme_stats,
        top_themes=top_themes,
        stale_warning=stale_warning,
    )
    report_path = save_report(report)
    print_status(f"筛选报告已生成：{report_path}")
    print_status(f"入选重点观察：{len(selected)} 只。")
    for row in selected:
        print_status(f"- {row.name}({row.item.code}) {fmt_number(row.change_pct, '%')}：{'、'.join(row.reasons[:3])}")

    can_continue = args.allow_stale or fresh is not False or not require_fresh
    if args.screen_only:
        can_continue = False

    if args.push and can_continue:
        markdown = build_dingtalk_markdown(selected, theme_stats, top_themes, report_path)
        send_dingtalk(markdown)
    elif args.push:
        print_status("已跳过推送。")

    command_results: dict[str, int] = {}
    if args.run_market and can_continue:
        market_args = ["main.py", "--market-review"]
        if args.force_run:
            market_args.append("--force-run")
        command_results["market"] = run_subprocess(market_args)

    if args.run_ai and can_continue and selected:
        stock_codes = ",".join(row.item.code for row in selected)
        ai_args = ["main.py", "--no-market-review", "--single-notify", "--stocks", stock_codes]
        if args.force_run:
            ai_args.append("--force-run")
        command_results["ai"] = run_subprocess(ai_args)
    elif args.run_ai and not selected:
        print_status("没有入选个股，跳过个股 AI 分析。")

    write_run_log(
        {
            "report": str(report_path),
            "watchlist_count": len(watchlist),
            "candidate_count": len(candidates),
            "selected": [row.item.code for row in selected],
            "fresh": fresh,
            "command_results": command_results,
        }
    )

    failed = {name: code for name, code in command_results.items() if code != 0}
    if failed:
        print_status(f"部分后续任务失败：{failed}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print_status(redact(f"执行失败：{exc}"))
        raise SystemExit(1)

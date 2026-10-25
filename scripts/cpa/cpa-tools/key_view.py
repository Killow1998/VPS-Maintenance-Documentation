"""Compact, secret-free presentation shared by the CLI and curses TUI."""
import shutil
import unicodedata
import datetime as dt
import math
from zoneinfo import ZoneInfo


def char_width(char):
    return 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1


def width(text):
    return sum(char_width(c) for c in str(text))


def clean(text):
    return "".join(c if c.isprintable() else " " for c in str(text))


def clip(text, size):
    result, used = [], 0
    for char in clean(text):
        amount = char_width(char)
        if used + amount > size:
            break
        result.append(char)
        used += amount
    return "".join(result)


def beijing(value):
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
    except (ValueError, TypeError, AttributeError):
        return "未知"


def quota_label(name):
    for prefix, label in (("five_hour", "5 小时"), ("seven_day", "7 天")):
        if name == prefix:
            return label
        if name.startswith(prefix + "_"):
            return label + " " + name[len(prefix) + 1:].replace("_", " ").title()
    return name


def remaining_value(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100 else None


def quota_bar(value, size=24):
    value = remaining_value(value)
    if value is None:
        return "[" + "未知".center(max(2, size - 2), "·") + "]"
    filled = int(size * value / 100 + .5)
    return "[" + "█" * filled + "░" * (size - filled) + "]"


def quota_lines(data):
    fresh = data.get("accounts_with_fresh_quota", 0)
    result = [f"号池 {data.get('accounts_total', 0)} 个账号 · {data.get('accounts_enabled', 0)} 启用 · {fresh} 有新鲜额度",
              "更新 " + beijing(data.get("checked_at")) + (" · 部分数据过时/未知" if data.get("stale", True) else " · 数据新鲜"), ""]
    for name, window in data.get("windows", {}).items():
        lo, hi = window.get("min_remaining_percent"), window.get("max_remaining_percent")
        label = quota_label(name)
        value = "未知" if lo is None or hi is None else f"{lo:g}%～{hi:g}%"
        result.append(f"号池 {label}余量范围 {value} · {window.get('known_accounts', 0)} 个账号有新鲜数据")
    if data.get("windows"):
        result.append("")
    if not data.get("accounts"):
        result.append("尚未取得账号额度，请检查 observer 与上游授权。")
    for account in data.get("accounts", []):
        state = "已禁用" if not account.get("enabled") else "新鲜" if not account.get("stale", True) else "过时/未知"
        result.append(str(account.get("email") or account.get("id", "身份未知")) + " · " + state)
        if not account.get("windows"):
            result.append("  暂无额度窗口 · " + str(account.get("reason") or account.get("status") or "未知"))
        for name, window in account.get("windows", {}).items():
            label = cell(quota_label(name), 16)
            suffix = " 旧值" if account.get("stale", True) else ""
            remaining = remaining_value(window.get("remaining_percent"))
            value = "未知" if remaining is None else f"{remaining:.1f}%"
            result.append(f"  {label} {quota_bar(remaining)} {cell(value, 7)} 重置 {beijing(window.get('resets_at'))}{suffix}")
        result.append("")
    result.append("█ 剩余  ░ 已用 · 时间为北京时间 · 各账号百分比不可相加")
    return result


def cell(text, size):
    text = clip(text, size)
    return text + " " * max(0, size - width(text))


def number(value):
    if value is None:
        return "未知"
    return f"{value:,}" if isinstance(value, int) else str(value)


def money(totals):
    lo, hi = totals.get("estimated_cost_usd_low"), totals.get("estimated_cost_usd_high")
    if lo is None or hi is None:
        return "未知"
    return f"${lo:.4f}" if lo == hi else f"${lo:.4f}–{hi:.4f}"


def table_line(row, totals, size=110):
    name_size = max(14, min(27, size - 62))
    status = "有效" if row.get("active") is True else "已撤销" if row.get("active") is False else "—"
    values = [(row["name"], name_size), (status, 6),
              (number(totals.get("requests")), 8), (number(totals.get("failed_requests")), 6),
              (number(totals.get("total_tokens")), 14), (money(totals), 23)]
    return clip(" ".join(cell(value, n) for value, n in values), size)


def table_header(size=110):
    name_size = max(14, min(27, size - 62))
    return clip(" ".join(cell(value, n) for value, n in
                [("调用者", name_size), ("状态", 6), ("请求", 8), ("失败", 6),
                 ("Tokens", 14), ("估算 USD", 23)]), size)


def detail_lines(row, usage):
    totals = usage.get("totals", {})
    result = [row["name"] + (" · 有效" if row.get("active") else " · 已撤销"),
              "ID  " + row["id"],
              "请求 " + number(totals.get("requests")) + "  失败 " + number(totals.get("failed_requests")) +
              "  Tokens " + number(totals.get("total_tokens")) + "  估算 " + money(totals),
              "输入 " + number(totals.get("input_tokens")) + "  输出 " + number(totals.get("output_tokens")),
              "缓存读取 " + number(totals.get("cache_read_tokens")) + "  缓存写入 " + number(totals.get("cache_write_tokens")),
              "采集 " + str(usage.get("collector_status") or "未知") +
              "  最近 " + str(usage.get("last_collected_at") or "未知"), "", "模型 / 请求 / Tokens / 估算 USD"]
    for model in usage.get("models", []):
        result.append(f"{model['model']}  {number(model.get('requests'))}  {number(model.get('total_tokens'))}  {money(model)}")
    result.extend(["", "估算基于 API 标价，不是订阅实际扣款。未知不等于零。"])
    return result


def render_cli(result):
    size = max(80, min(120, shutil.get_terminal_size((110, 24)).columns))
    if "quota" in result:
        print("\n".join(quota_lines(result["quota"])))
    elif "keys" in result:
        print(cell("调用者", 27) + cell("状态", 10) + "ID（短）")
        for row in result["keys"]:
            print(cell(row["name"], 27) + cell("有效" if row["active"] else "已撤销", 10) + row["id"][:12])
        print(str(len(result["keys"])) + " 个 Key · 完整 ID 用 --json 查看")
    elif "usage" in result:
        print(table_header(size))
        for usage in result["usage"]:
            print(table_line({"name": usage["client"], "active": None}, usage["totals"], size))
        print("北京时间 · API 标价估算；未知不等于零")
    else:
        print(result.get("message", "完成"))
        if result.get("env_file"):
            print("CPA env：" + result["env_file"])
        if result.get("reload_required"):
            print("需要在当前终端使用时，source 上述文件。")

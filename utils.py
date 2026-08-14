# utils.py
import json
import os

from astrbot.api import logger


def persist_restart_cache(config) -> None:
    """将 restart_cache 直接写入配置文件，绕过 save_config 的深层快照问题。"""
    try:
        cache_data = config.get("restart_cache")
        if cache_data is None:
            return
        cfg_path = config.config_path
        with open(cfg_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        raw["restart_cache"] = cache_data
        with open(cfg_path, "w", encoding="utf-8-sig") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[重启插件] 持久化 restart_cache 失败: {e}")


def seconds_to_cron(seconds: int) -> str | None:
    """将秒数转成Cron表达式，>=60秒才可转，<60秒返回None"""
    if seconds < 60:
        return None
    if seconds % 3600 == 0:
        return f"0 */{seconds // 3600} * * *"
    minutes = seconds // 60
    return f"*/{minutes} * * * *"


def cron_to_human(cron: str) -> str:
    """
    将 5 段 cron（分 时 日 月 周）转换为中文易读描述
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        raise ValueError("Cron 表达式必须是 5 段（分 时 日 月 周）")

    minute, hour, day, month, week = parts

    def parse_field(val, unit, names=None):
        if val == "*":
            return f"每{unit}"
        if val.startswith("*/"):
            return f"每{val[2:]}{unit}"
        if "," in val:
            items = val.split(",")
            return "、".join(
                names.get(i, f"{i}{unit}") if names else f"{i}{unit}" for i in items
            )
        if "-" in val:
            start, end = val.split("-")
            if names:
                return f"{names[start]}至{names[end]}"
            return f"{start}到{end}{unit}"
        return names.get(val, f"{val}{unit}") if names else f"{val}{unit}"

    week_names = {
        "0": "周日",
        "1": "周一",
        "2": "周二",
        "3": "周三",
        "4": "周四",
        "5": "周五",
        "6": "周六",
    }

    desc = []

    # 周
    if week != "*":
        desc.append(parse_field(week, "", week_names))

    # 月
    if month != "*":
        desc.append(parse_field(month, "月"))

    # 日
    if day != "*":
        desc.append(parse_field(day, "日"))
    elif week == "*":
        desc.append("每天")

    # 时间
    if hour == "*" and minute == "*":
        desc.append("每分钟")
    else:
        time_desc = []
        if hour != "*":
            time_desc.append(parse_field(hour, "点"))
        if minute != "*":
            time_desc.append(parse_field(minute, "分"))
        desc.append(" ".join(time_desc))

    return " ".join(desc)


def fmt_seconds(seconds: float) -> str:
    """将秒数格式化为 时:分:秒 中文描述"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}小时{m}分{s}秒"


def get_memory_info(decimal_places=1):
    """
    获取当前设备内存情况，支持自定义小数位数

    Args:
        decimal_places (int): 小数位数，默认为1位

    Returns:
        str: 已用内存/总内存(百分比) 格式，如 "8.5GB/16.0GB(53.2%)"
    """
    import psutil
    # 获取内存信息
    memory = psutil.virtual_memory()

    # 计算已用内存 (总内存 - 可用内存)
    total_memory = memory.total
    used_memory = total_memory - memory.available

    # 转换为GB单位
    total_gb = total_memory / (1024**3)
    used_gb = used_memory / (1024**3)

    # 计算使用百分比
    usage_percent = (used_memory / total_memory) * 100

    # 格式化输出，使用指定的小数位数
    format_str = f"{{:.{decimal_places}f}}GB/{{:.{decimal_places}f}}GB({{:.1f}}%)"
    return format_str.format(used_gb, total_gb, usage_percent)


def get_memory_usage_percent() -> float:
    """
    获取当前内存使用百分比（0~100）

    Returns:
        float: 内存使用百分比
    """
    import psutil

    memory = psutil.virtual_memory()
    return memory.percent


def get_memory_usage_mb() -> float:
    """
    获取当前已用内存（MB）

    Returns:
        float: 已用内存（MB），不含 cache/buffer
    """
    import psutil

    memory = psutil.virtual_memory()
    used_mb = (memory.total - memory.available) / (1024**2)
    return used_mb


def is_memory_over_threshold(threshold_value, threshold_unit):
    """
    判断当前内存占用是否超过阈值

    Args:
        threshold_value (int/float): 阈值数值
        threshold_unit (str): 'percent' 或 'mb'

    Returns:
        tuple[bool, float]: (是否超过阈值, 当前内存值)
    """
    unit = str(threshold_unit).strip().lower()
    if unit == "mb":
        current = get_memory_usage_mb()
    else:  # 默认 percent
        current = get_memory_usage_percent()
    try:
        threshold = float(threshold_value)
    except (TypeError, ValueError):
        threshold = 0
    return current > threshold, current


def format_clear_summary(cleared: int, total: int, group_umos: list, private_umos: list, now_str: str) -> str:
    """
    格式化清空上下文的输出，包含详细的私聊/群聊会话列表
    """
    lines = []
    lines.append(f"已一键清空 {cleared}/{total} 个会话上下文")

    if private_umos:
        lines.append("私聊会话：")
        for idx, umo in enumerate(private_umos, 1):
            if not umo:
                continue
            parts = umo.split(":")
            qq = parts[2] if len(parts) >= 3 else umo
            lines.append(f"  {idx}. {qq}")

    if group_umos:
        lines.append("群聊会话：")
        for idx, umo in enumerate(group_umos, 1):
            if not umo:
                continue
            parts = umo.split(":")
            gid = parts[2] if len(parts) >= 3 else umo
            lines.append(f"  {idx}. {gid}")

    lines.append(f"清除时间：{now_str}")
    return "\n".join(lines)

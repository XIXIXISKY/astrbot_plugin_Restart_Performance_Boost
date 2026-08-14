# restart_plugin.py
import asyncio
import json
import os
import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.event.filter import on_astrbot_loaded
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import File, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.star_manager import PluginManager

from .dashboard_client import DashboardClient
from .restart_scheduler import RestartScheduler
from .utils import fmt_seconds, get_memory_info, persist_restart_cache



class RestartPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.star_manager: PluginManager = self.context._star_manager
        self.config = config
        raw_cache = config.get("restart_cache")
        if isinstance(raw_cache, dict):
            self.cache: dict[str, Any] = raw_cache
        else:
            self.cache = {}
            config["restart_cache"] = self.cache

    # ================== 生命周期 ==================

    async def initialize(self):
        self.dashboard = DashboardClient(self.context)
        await self.dashboard.initialize()
        self.scheduler = RestartScheduler(
            self.context, self.config, self.dashboard, self.cache
        )
        await self.scheduler.start()


        interval = self.scheduler._get_interval()
        if interval > 0:
            logger.info(f"[重启插件] 自动重启已开启，间隔 {fmt_seconds(interval)}")
        else:
            logger.debug("[重启插件] 自动重启未开启")

    async def terminate(self):
        await self.dashboard.terminate()
        await self.scheduler.shutdown()
        logger.info("重启插件已终止")

    # ================== 重启完成通知 ==================

    @on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后发送内存反馈。

        注意：该钩子在平台 WebSocket 完成连接之前就会触发，
        因此必须带重试等待平台就绪，否则发送会静默失败。
        """
        import asyncio
        import time
        from .utils import get_memory_info, get_memory_usage_percent

        # 耗时 = 现在 - 重启发起时刻
        start_ts = float(self.cache.get("start_ts") or 0)
        elapsed = (time.time() - start_ts) if start_ts > 0 else 0.0
        start_time_str = (
            time.strftime("%H:%M:%S", time.localtime(start_ts)) if start_ts > 0 else "--:--:--"
        )
        reason = str(self.cache.get("restart_reason") or "手动重启")
        trigger_mem = str(self.cache.get("trigger_mem") or "未知")
        msg = "AstrBot重启完成（耗时{:.2f}秒）\n".format(max(elapsed, 0))
        msg += "清除时间：{}\n".format(start_time_str)
        msg += "原因：{}\n".format(reason)
        msg += "触发时内存：{}\n".format(trigger_mem)
        msg += "当前内存：{}".format(get_memory_info())

        target = self.cache.get("umo") or "default:FriendMessage:1985895920"
        max_attempts = 30
        for attempt in range(1, max_attempts + 1):
            try:
                ok = await self.context.send_message(
                    session=target,
                    message_chain=MessageChain([Plain(msg)]),
                )
                if ok:
                    logger.info("[重启插件] 内存反馈发送成功")
                    return
                logger.warning(
                    f"[重启插件] 内存反馈未找到匹配平台({target})，"
                    f"第 {attempt}/{max_attempts} 次重试"
                )
            except Exception as e:
                logger.warning(
                    f"[重启插件] 内存反馈发送失败(第 {attempt}/{max_attempts} 次)：{e}"
                )
            await asyncio.sleep(5)
        logger.error("[重启插件] 内存反馈发送失败：重试 30 次后仍未成功")

    # ================== 手动重启命令 ==================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重启", alias={"restart"})
    async def restart_system(self, event: AstrMessageEvent):
        """重启Astrbot"""
        from .utils import get_memory_usage_percent
        await event.send(event.plain_result("正在重启 AstrBot…"))
        self.cache["platform_id"] = event.get_platform_id()
        self.cache["umo"] = event.unified_msg_origin
        self.cache["start_ts"] = time.time()
        self.cache["restart_reason"] = "手动重启"
        self.cache["trigger_mem"] = "{:.1f}%".format(get_memory_usage_percent())
        self.config.save_config()
        await self.dashboard.restart()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("定时重启")
    async def schedule_restart(self, event: AstrMessageEvent, mode: str | None = None):
        """定时重启 开/关/<秒数> —— 统一控制自动重启"""
        # 数字模式：设置间隔并开启
        if isinstance(mode, (int, float)) or (mode and str(mode).isdigit()):
            seconds = int(mode)
            self.config["auto_restart_interval_seconds"] = max(seconds, 0)
            # 保存用户会话信息
            self.cache["platform_id"] = event.get_platform_id()
            self.cache["umo"] = event.unified_msg_origin
            self.cache["start_ts"] = 0
            self.config.save_config()
            # 重载调度器（同时重载内存监控配置）
            await self.scheduler.reload_memory_monitor()
            await self.scheduler.reload_interval()
            if seconds <= 0:
                yield event.plain_result("已关闭自动重启")
            else:
                yield event.plain_result(
                    f"已开启自动重启：每{fmt_seconds(seconds)}重启一次"
                )
            return

        # 开关模式
        if mode not in ["开", "关"]:
            await event.send(event.plain_result("正确格式：定时重启 开/关/<秒数>"))
            return
        if mode == "开":
            interval = self.scheduler._get_interval()
            if interval <= 0:
                yield event.plain_result("请先设置间隔：定时重启 <秒数>")
                return
            await self.scheduler.reload_interval()
            yield event.plain_result(f"已开启自动重启：每{fmt_seconds(interval)}重启一次")
        else:
            self.config["auto_restart_interval_seconds"] = 0
            self.config.save_config()
            await self.scheduler.stop_interval()
            yield event.plain_result("已关闭自动重启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("定时重置")
    async def ctx_reset_cmd(self, event: AstrMessageEvent, time_str: str | None = None):
        """定时重置 <HH:MM|关> —— 设置每日定时重置上下文的时间"""
        if time_str is None:
            cur = self.config.get("ctx_reset_time", "")
            if cur:
                yield event.plain_result(f"当前定时重置时间：{cur}，输入「定时重置 关」关闭")
            else:
                yield event.plain_result("当前定时重置未开启，格式：定时重置 <HH:MM>")
            return
        if time_str == "关":
            self.config["ctx_reset_time"] = ""
            self.config.save_config()
            await self.scheduler.reload_ctx_reset()
            yield event.plain_result("已关闭每日定时重置上下文")
            return
        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            yield event.plain_result("时间格式错误，请使用 HH:MM 24小时制，如 23:00")
            return
        self.config["ctx_reset_time"] = time_str
        self.config.save_config()
        # 保存平台ID
        platform_id = event.get_platform_id()
        self.cache["ctx_reset_platform_id"] = platform_id
        self.config.save_config()

        await self.scheduler.reload_ctx_reset()
        yield event.plain_result(f"已设置每日 {time_str} 自动重置上下文")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清空所有会话上下文", alias={"清理全部", "一键reset"})
    async def clear_all_groups_context(self, event: AstrMessageEvent):
        """一键清空所有已登记会话的上下文（群聊 + 私聊）"""
        from astrbot.core.utils.active_event_registry import active_event_registry

        def _load_list(raw):
            try:
                lst = json.loads(raw) if raw else []
                if isinstance(lst, str):
                    lst = [lst]
            except (json.JSONDecodeError, TypeError):
                lst = [raw] if raw else []
            return [u for u in lst if u]

        # 分别保留群聊和私聊名单
        group_umos = _load_list(self.cache.get("ctx_reset_umo"))
        private_umos = _load_list(self.cache.get("ctx_reset_private_umo"))
        umo_list = group_umos + private_umos
        if not umo_list:
            yield event.plain_result("未登记任何会话，先执行「定时重置 <HH:MM>」或「增加会话」再试")
            return

        cleared = 0
        for umo in umo_list:
            try:
                active_event_registry.stop_all(umo)
            except Exception:
                pass
            try:
                cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                if cid:
                    await self.context.conversation_manager.update_conversation(umo, cid, [])
                    cleared += 1
            except Exception as e:
                logger.error(f"[重启插件] 清理 {umo} 失败：{e}")
        now_str = datetime.now().strftime("%H:%M:%S")
        from .utils import format_clear_summary
        yield event.plain_result(format_clear_summary(cleared, len(umo_list), group_umos, private_umos, now_str))

    # ============ 会话管理指令 ============

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看列表")
    async def list_sessions(self, event: AstrMessageEvent):
        """查看当前已登记的所有群聊和私聊"""
        platform_id = self.cache.get("ctx_reset_platform_id") or event.get_platform_id()
        # 保存平台ID
        self.cache["ctx_reset_platform_id"] = platform_id
        platform = self.context.get_platform_inst(platform_id)
        bot = getattr(platform, "bot", None) if platform else None

        # --- 读取群聊 ---
        raw_umo = self.cache.get("ctx_reset_umo")
        group_lines = []
        try:
            umo_list = json.loads(raw_umo) if raw_umo else []
            if isinstance(umo_list, str):
                umo_list = [umo_list]
        except (json.JSONDecodeError, TypeError):
            umo_list = [raw_umo] if raw_umo else []
        umo_list = [u for u in umo_list if u]
        for idx, umo in enumerate(umo_list, 1):
            parts = umo.split(":")
            gid = parts[2] if len(parts) >= 3 else umo
            try:
                gid_int = int(gid)
                if bot:
                    info = await bot.call_action("get_group_info", group_id=gid_int)
                    name = info.get("group_name", f"未知群({gid_int})")
                else:
                    name = f"未知群({gid_int})"
            except Exception:
                name = f"未知群({gid})"
            group_lines.append(f"{idx}.{name} {gid}")

        # --- 读取私聊 ---
        raw_private = self.cache.get("ctx_reset_private_umo")
        private_lines = []
        try:
            p_list = json.loads(raw_private) if raw_private else []
            if isinstance(p_list, str):
                p_list = [p_list]
        except (json.JSONDecodeError, TypeError):
            p_list = [raw_private] if raw_private else []
        p_list = [u for u in p_list if u]
        for idx, umo in enumerate(p_list, 1):
            parts = umo.split(":")
            qq = parts[2] if len(parts) >= 3 else umo
            try:
                if bot:
                    info = await bot.call_action("get_stranger_info", user_id=int(qq))
                    nickname = info.get("nickname", f"未知({qq})")
                else:
                    nickname = f"未知({qq})"
            except Exception:
                nickname = f"未知({qq})"
            private_lines.append(f"{idx}.{nickname} {qq}")

        # --- 组装输出 ---
        result_parts = []
        if group_lines:
            result_parts.append("列表内群聊：")
            result_parts.extend(group_lines)
        if private_lines:
            if result_parts:
                result_parts.append("")
            result_parts.append("列表内私聊：")
            result_parts.extend(private_lines)
        if not result_parts:
            yield event.plain_result("当前未登记任何会话")
            return
        yield event.plain_result("\n".join(result_parts))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("增加会话")
    async def add_session(self, event: AstrMessageEvent, session_type: str | None = None, session_id: str | None = None):
        """增加会话 群聊/私聊 <QQ号/群号>：为定时重置名单添加会话"""
        if not session_type or not session_id:
            yield event.plain_result("正确格式：增加会话 群聊 <群号> 或 增加会话 私聊 <QQ号>")
            return

        platform_id = self.cache.get("ctx_reset_platform_id") or event.get_platform_id()
        # 保存平台ID
        self.cache["ctx_reset_platform_id"] = platform_id

        if session_type == "群聊":
            # 群聊
            try:
                gid = str(int(session_id))
            except ValueError:
                yield event.plain_result(f"无效的群号：{session_id}")
                return
            umo = f"{platform_id}:GroupMessage:{gid}"
            cache_key = "ctx_reset_umo"
            label = f"群 {gid}"
        elif session_type == "私聊":
            try:
                qq = str(int(session_id))
            except ValueError:
                yield event.plain_result(f"无效的QQ号：{session_id}")
                return
            umo = f"{platform_id}:FriendMessage:{qq}"
            cache_key = "ctx_reset_private_umo"
            label = f"私聊 {qq}"
        else:
            yield event.plain_result("类型错误，请使用「群聊」或「私聊」")
            return

        # 保存平台ID（如果还没保存）
        if not self.cache.get("ctx_reset_platform_id"):
            self.cache["ctx_reset_platform_id"] = platform_id

        raw = self.cache.get(cache_key)
        try:
            umo_list = json.loads(raw) if raw else []
            if isinstance(umo_list, str):
                umo_list = [umo_list]
        except (json.JSONDecodeError, TypeError):
            umo_list = [raw] if raw else []
        umo_list = [u for u in umo_list if u]

        if umo in umo_list:
            yield event.plain_result(f"该{label}已在名单中")
            return

        umo_list.append(umo)
        self.cache[cache_key] = json.dumps(umo_list, ensure_ascii=False)
        self.config.save_config()
        yield event.plain_result(f"已将{label}加入名单，当前共 {len(umo_list)} 个{session_type}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除会话")
    async def remove_session(self, event: AstrMessageEvent, session_type: str | None = None, session_id: str | None = None):
        """删除会话 群聊/私聊 <QQ号/群号>：从定时重置名单移除会话"""
        if not session_type or not session_id:
            yield event.plain_result("正确格式：删除会话 群聊 <群号> 或 删除会话 私聊 <QQ号>")
            return

        platform_id = self.cache.get("ctx_reset_platform_id") or event.get_platform_id()
        # 保存平台ID
        self.cache["ctx_reset_platform_id"] = platform_id

        # 读取当前名单
        cache_key = "ctx_reset_umo" if session_type == "群聊" else "ctx_reset_private_umo"
        raw = self.cache.get(cache_key)
        try:
            umo_list = json.loads(raw) if raw else []
            if isinstance(umo_list, str):
                umo_list = [umo_list]
        except (json.JSONDecodeError, TypeError):
            umo_list = [raw] if raw else []
        umo_list = [u for u in umo_list if u]

        # 先试试按序号删除（输入的是纯数字且小于等于列表长度）
        sid_str = str(session_id) if session_id is not None else ""
        if sid_str.isdigit() and int(sid_str) <= len(umo_list):
            idx = int(sid_str) - 1
            removed_umo = umo_list.pop(idx)
            # 从UMO里提取QQ或群号做显示
            parts = removed_umo.split(":")
            uid = parts[2] if len(parts) >= 3 else removed_umo
            label = f"{session_type} {uid}（序号{sid_str}）"
            self.cache[cache_key] = json.dumps(umo_list, ensure_ascii=False)
            self.config.save_config()
            yield event.plain_result(f"已将{label}移出名单，当前共 {len(umo_list)} 个{session_type}")
            return

        if session_type == "群聊":
            try:
                gid = str(int(session_id))
            except ValueError:
                yield event.plain_result(f"无效的群号：{session_id}")
                return
            umo = f"{platform_id}:GroupMessage:{gid}"
            label = f"群 {gid}"
        elif session_type == "私聊":
            try:
                qq = str(int(session_id))
            except ValueError:
                yield event.plain_result(f"无效的QQ号：{session_id}")
                return
            umo = f"{platform_id}:FriendMessage:{qq}"
            label = f"私聊 {qq}"
        else:
            yield event.plain_result("类型错误，请使用「群聊」或「私聊」")
            return

        if not umo_list:
            yield event.plain_result(f"当前未登记任何{session_type}")
            return

        if umo not in umo_list:
            yield event.plain_result(f"该{label}不在名单中")
            return

        umo_list.remove(umo)
        self.cache[cache_key] = json.dumps(umo_list, ensure_ascii=False)
        self.config.save_config()
        yield event.plain_result(f"已将{label}移出名单，当前共 {len(umo_list)} 个{session_type}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("内存阈值", alias={"内存监控阈值", "mem阈值"})
    async def set_memory_threshold(self, event: AstrMessageEvent, value: GreedyStr):
        """内存阈值 <数值[单位]|<开|关|状态> —— 设定内存监控阈值并支持单位"""
        if value in ["", "?", "状态", "查看"]:
            mconf = self.scheduler._get_memory_config()
            if mconf["enabled"]:
                yield event.plain_result(
                    f"内存监控当前状态：开启\n"
                    f"阈值：{mconf['threshold_value']}{self._unit_show(mconf['threshold_unit'])}\n"
                    f"检查间隔：{int(mconf['check_interval'])}秒，冷却：{int(mconf['cooldown_seconds'])}秒"
                )
            else:
                yield event.plain_result("内存监控当前状态：关闭")
            return

        if value in ["开", "on"]:
            self.config["memory_monitor_enabled"] = True
            self.config.save_config()
            await self.scheduler.reload_memory_monitor()
            mconf = self.scheduler._get_memory_config()
            yield event.plain_result(
                f"已开启内存监控（阈值 {mconf['threshold_value']}{self._unit_show(mconf['threshold_unit'])})"
            )
            return

        if value in ["关", "off"]:
            self.config["memory_monitor_enabled"] = False
            self.config.save_config()
            await self.scheduler.reload_memory_monitor()
            yield event.plain_result("已关闭内存监控")
            return

        # 数字模式：设置阈值（支持单位后缀；无后缀按当前默认单位）
        num, unit, err, explicit = self._parse_threshold_input(value)
        if err:
            yield event.plain_result(err)
            return
        num = int(num) if num.is_integer() else num
        self.config["memory_threshold_value"] = num
        self.config["memory_threshold_unit"] = unit
        self.config["memory_monitor_enabled"] = True
        self.config.save_config()
        await self.scheduler.reload_memory_monitor()
        unit_show = "%" if unit == "percent" else "MB"
        unit_note = "单位已切换为 {unit}" if explicit else "按默认单位 {unit} 解释"
        yield event.plain_result(f"已设定内存阈值：超过 {num}{unit_show} 时自动重启（{unit_note.format(unit=unit)}）")
        return

    def _parse_threshold_input(self, raw: str):
        """解析内存阈值输入 → (数值, 单位, 错误说明, 是否显式带单位)

        规则：
        - 带后缀如 `85 percent` / `2048 mb` / `85%` / `2048mb` → 按后缀单位
        - 只输数字如 `85` → 按当前默认单位（memory_threshold_unit）解释
        """
        s = str(raw).strip().lower().replace("，", ",")
        m = re.match(r"^(\d+(?:\.\d+)?)\s*(percent|pct|%|百分比|mb|m|兆)?$", s)
        if not m:
            return None, None, "无法识别的格式，示例：内存阈值 85 / 内存阈值 2048 mb", False
        num = float(m.group(1))
        suffix = m.group(2)
        explicit = suffix is not None
        cur_unit = str(self.config.get("memory_threshold_unit", "percent")).lower()
        if suffix in ("mb", "m", "兆"):
            unit = "mb"
        elif suffix in ("percent", "pct", "%", "百分比"):
            unit = "percent"
        else:
            unit = cur_unit  # 无后缀 → 按当前默认单位
        # 数值校验
        if unit == "percent":
            if not (0 < num <= 100):
                return None, None, "百分比阈值需在 1~100 之间", False
        else:
            total_mb = 0
            try:
                import psutil
                total_mb = psutil.virtual_memory().total / 1024 / 1024
            except Exception:
                pass
            if total_mb and not (0 < num <= total_mb):
                return None, None, f"MB 阈值需在 1~{int(total_mb)} 之间（本机物理内存 {int(total_mb)}MB）", False
            if not (0 < num):
                return None, None, "MB 阈值需大于 0", False
        return num, unit, None, explicit

    @staticmethod
    def _unit_show(unit: str) -> str:
        """单位展示：percent → %，mb → MB"""
        return "%" if str(unit).lower() == "percent" else "MB"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("内存单位", alias={"阈值单位", "mem单位"})
    async def set_memory_unit(self, event: AstrMessageEvent, value: str | None = None):
        """内存单位 <percent|mb|状态> —— 切换内存阈值的默认单位"""
        if value is None or value in ["?", "状态", "查看"]:
            cur = str(self.config.get("memory_threshold_unit", "percent")).lower()
            cur_show = "%" if cur == "percent" else "MB"
            yield event.plain_result(
                f"当前默认单位：{cur}（{cur_show}）\
"
                f"规则：只输数字按默认单位，如 `内存阈值 85`；\
"
                f"带后缀优先，如 `内存阈值 2048 mb`。"
            )
            return
        v = str(value).strip().lower()
        if v in ("percent", "pct", "%", "百分比"):
            unit = "percent"
        elif v in ("mb", "m", "兆"):
            unit = "mb"
        else:
            yield event.plain_result("正确格式：内存单位 <percent|mb|状态>")
            return
        self.config["memory_threshold_unit"] = unit
        self.config.save_config()
        yield event.plain_result(f"内存阈值默认单位已切换为：{unit}（以后只输数字就按这个单位算喵）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("内存监控")
    async def memory_monitor_combined(self, event: AstrMessageEvent, value: GreedyStr):
        """内存监控 <数值[单位]|开|关|状态> —— 设置内存阈值并自动打开监控（一条指令搞定）"""
        if value in ["", "?", "状态", "查看"]:
            mconf = self.scheduler._get_memory_config()
            if mconf["enabled"]:
                yield event.plain_result("内存监控：开启\n阈值：{}{}\n检查间隔：{}秒，冷却：{}秒".format(
                    mconf['threshold_value'], self._unit_show(mconf['threshold_unit']),
                    int(mconf['check_interval']), int(mconf['cooldown_seconds'])
                ))
            else:
                yield event.plain_result("内存监控：关闭")
            return

        if value in ["开", "on"]:
            self.config["memory_monitor_enabled"] = True
            self.config.save_config()
            await self.scheduler.reload_memory_monitor()
            mconf = self.scheduler._get_memory_config()
            yield event.plain_result(
                f"已开启内存监控（阈值 {mconf['threshold_value']}{self._unit_show(mconf['threshold_unit'])})"
            )
            return

        if value in ["关", "off"]:
            self.config["memory_monitor_enabled"] = False
            self.config.save_config()
            await self.scheduler.reload_memory_monitor()
            yield event.plain_result("已关闭内存监控")
            return

        # 数字模式：设阈值并自动开启监控（支持单位后缀，无后缀按默认单位）
        num, unit, err, explicit = self._parse_threshold_input(value)
        if err:
            yield event.plain_result(err)
            return
        num = int(num) if num.is_integer() else num
        self.config["memory_threshold_value"] = num
        self.config["memory_threshold_unit"] = unit
        self.config["memory_monitor_enabled"] = True
        self.config.save_config()
        await self.scheduler.reload_memory_monitor()
        unit_show = "%" if unit == "percent" else "MB"
        unit_note = "单位已切换为 {unit}" if explicit else "按默认单位 {unit} 解释"
        yield event.plain_result(f"已开启内存监控，阈值：超过 {num}{unit_show} 时自动重启（{unit_note.format(unit=unit)}）")
        return

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("内存检查频率", alias={"内存检查间隔", "检查间隔", "mem间隔"})
    async def set_memory_check_interval(self, event: AstrMessageEvent, value: str | None = None):
        """内存检查间隔 <秒数|默认> —— 设置内存检查频率（最少5秒）"""
        if value is None or value in ["?", "状态", "查看"]:
            mconf = self.scheduler._get_memory_config()
            yield event.plain_result(f"当前内存检查间隔：{int(mconf['check_interval'])}秒")
            return
        if value in ["默认", "reset", "恢复"]:
            self.config["memory_check_interval"] = 30
            self.config.save_config()
            await self.scheduler.reload_memory_monitor()
            yield event.plain_result("内存检查间隔已恢复默认 30 秒")
            return
        if str(value).isdigit():
            num = int(value)
            if num < 5:
                yield event.plain_result("检查间隔至少 5 秒，太频繁会把锅烧穿的喵～")
                return
            self.config["memory_check_interval"] = num
            self.config.save_config()
            await self.scheduler.reload_memory_monitor()
            yield event.plain_result(f"内存检查间隔已设为 {num} 秒")
            return
        yield event.plain_result("正确格式：内存检查间隔 <秒数|默认>")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("内存冷却", alias={"重启冷却", "mem冷却"})
    async def set_memory_cooldown(self, event: AstrMessageEvent, value: str | None = None):
        """内存冷却 <秒数|默认> —— 设置内存触发重启后的冷却时间"""
        if value is None or value in ["?", "状态", "查看"]:
            mconf = self.scheduler._get_memory_config()
            yield event.plain_result(f"当前重启冷却时间：{int(mconf['cooldown_seconds'])}秒")
            return
        if value in ["默认", "reset", "恢复"]:
            self.config["memory_cooldown_seconds"] = 600
            self.config.save_config()
            await self.scheduler.reload_memory_monitor()
            yield event.plain_result("重启冷却已恢复默认 600 秒（10分钟）")
            return
        if str(value).isdigit():
            num = int(value)
            self.config["memory_cooldown_seconds"] = num
            self.config.save_config()
            await self.scheduler.reload_memory_monitor()
            yield event.plain_result(f"重启冷却已设为 {num} 秒")
            return
        yield event.plain_result("正确格式：内存冷却 <秒数|默认>")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置自动重启", alias={"reset_auto_restart"})
    async def reset_auto_restart(self, event: AstrMessageEvent):
        """重置自动重启倒计时（同时重载内存监控配置）"""
        # 重载内存监控配置
        await self.scheduler.reload_memory_monitor()

        # 重载间隔重启
        interval = self.scheduler._get_interval()
        if interval > 0:
            await self.scheduler.reload_interval()
            yield event.plain_result(
                f"已重置倒计时，每{fmt_seconds(interval)}重启一次"
            )
        else:
            yield event.plain_result("自动重启未开启，请先设置：定时重启 <秒数>")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重载")
    async def reload_plugin(
        self, event: AstrMessageEvent, target: str | int | None = None
    ):
        """重载 <插件名|序号|空|all>"""
        from astrbot.core.star.star import star_registry as sr

        visible = [m for m in sr if not m.reserved]
        if not visible:
            yield event.plain_result("暂无插件")
            return

        if target is None:
            lines = ["需指定插件序号："]
            for idx, meta in enumerate(visible, start=1):
                show = meta.display_name or meta.name
                lines.append(f"{idx}. {show}")
            await event.send(event.plain_result("\n".join(lines)))
            return

        plugin_key = None
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
            idx = int(target) - 1
            if 0 <= idx < len(visible):
                plugin_key = visible[idx].name
            else:
                yield event.plain_result("序号超出范围")
                return
        elif str(target).lower() == "all":
            plugin_key = None
        else:
            tgt = str(target)
            for meta in sr:
                if tgt in str(meta.display_name) or tgt in str(meta.name):
                    plugin_key = meta.name
                    break
            if plugin_key is None:
                yield event.plain_result("未找到该插件")
                return

        success, error_message = await self.star_manager.reload(plugin_key)

        if plugin_key is None:
            show_name = "所有插件"
        else:
            show_name = plugin_key
            if meta := next(
                (m for m in sr if (m.name or m.module_path) == plugin_key), None
            ):
                show_name = str(meta.display_name or meta.name).removeprefix(
                    "astrbot_plugin_"
                )

        if success:
            yield event.plain_result(f"{show_name}重载成功")
        else:
            yield event.plain_result(f"{show_name}重载失败：{error_message}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("插件状态")
    async def restart_status(self, event: AstrMessageEvent):
        """查看插件状态"""
        from .utils import get_memory_usage_percent, get_memory_usage_mb

        lines = ["=== 插件状态 ==="]

        # 间隔重启
        interval = self.scheduler._get_interval()
        if interval > 0:
            lines.append(f"✅ 定时重启：开启，每{fmt_seconds(interval)}")
        else:
            lines.append("❌ 定时重启：关闭")

        # 内存监控
        mconf = self.scheduler._get_memory_config()
        if mconf["enabled"]:
            lines.append(
                f"✅ 内存监控：开启（阈值 {mconf['threshold_value']}{self._unit_show(mconf['threshold_unit'])}，"
                f"每{int(mconf['check_interval'])}秒检查，冷却{int(mconf['cooldown_seconds'])}秒）"
            )
        else:
            lines.append("❌ 内存监控：关闭")

        # 每日定时重置上下文
        ctx_reset_time = self.scheduler._get_ctx_reset_time()
        running = self.scheduler._ctx_reset_running if hasattr(self.scheduler, '_ctx_reset_running') else False
        has_platform = bool(self.cache.get("ctx_reset_platform_id"))
        has_group = bool(self.cache.get("ctx_reset_umo"))
        has_private = bool(self.cache.get("ctx_reset_private_umo"))
        if ctx_reset_time:
            loop_status = "🟢运行中" if running else "🔴未启动"
            lines.append(f"✅ 定时重置上下文：每天 {ctx_reset_time}（{loop_status}）")
            lines.append(f"   📋 平台ID：{'已保存' if has_platform else '❌未保存'}  |  群聊名单：{'有' if has_group else '无'}  |  私聊名单：{'有' if has_private else '无'}")
        else:
            lines.append("❌ 定时重置上下文：关闭")

        # 当前内存
        try:
            mem_pct = get_memory_usage_percent()
            mem_mb = get_memory_usage_mb()
            lines.append(f"📊 当前内存：{mem_pct:.1f}%（{mem_mb:.0f}MB）")
        except Exception:
            lines.append("📊 当前内存：读取失败")

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("性能")
    async def performance_info(self, event: AstrMessageEvent):
        """查看系统性能占用"""
        import psutil
        from datetime import datetime

        # CPU
        cpu_percent = psutil.cpu_percent(interval=1)
        load_avg = psutil.getloadavg()

        # 内存
        mem = psutil.virtual_memory()
        total_mb = mem.total / 1024 / 1024
        used_mb = (mem.total - mem.available) / 1024 / 1024
        free_mb = mem.available / 1024 / 1024
        used_pct = mem.percent
        free_pct = 100 - used_pct

        # 时间
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            f"负载情况：{load_avg[0]:.2f} {load_avg[1]:.2f} {load_avg[2]:.2f}",
            f"CPU占用率：{cpu_percent:.1f}%",
            f"内存已使用：{used_mb:.1f}MB",
            f"（占用率：{used_pct:.1f}%）",
            f"内存空闲：{free_mb:.1f}MB",
            f"（占用率：{free_pct:.1f}%）",
            f"总内存：{total_mb:.1f}MB",
            f"查看时间：{now}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("帮助", alias={"help", "指令列表", "命令列表"})
    async def help_command(self, event: AstrMessageEvent):
        """查看"重启插件"的全部指令及用法。"""
        lines = [
            "重启插件增强版 指令列表+用法格式",
            "",
            "1️⃣ 重启",
            "   用法：重启",
            "   说明：立即重启 AstrBot 核心（需管理员）",
            "",
            "2️⃣ 定时重启",
            "   用法：定时重启 开 / 关 / <秒数>",
            "   示例：定时重启 7200  （每2小时重启一次）",
            "   示例：定时重启 关",
            "",
            "3️⃣ 定时重置",
            "   用法：定时重置 <HH:MM> / 关",
            "   示例：定时重置 04:00  （每日凌晨4点重置上下文）",
            "   示例：定时重置 关",
            "",
            "4️⃣ 清空所有会话上下文",
            "   用法：清空所有会话上下文",
            "   说明：一键清空所有已登记会话的上下文",
            "",
            "5️⃣ 查看列表",
            "   用法：查看列表",
            "   说明：查看当前已登记的所有群聊和私聊会话",
            "",
            "6️⃣ 增加会话",
            "   用法：增加会话 群聊 <群号>",
            "   用法：增加会话 私聊 <QQ号>",
            "   示例：增加会话 群聊 123456789",
            "   说明：为定时重置名单添加会话",
            "",
            "7️⃣ 删除会话",
            "   用法：删除会话 群聊 <群号>",
            "   用法：删除会话 私聊 <QQ号>",
            "   示例：删除会话 私聊 987654321",
            "   说明：从定时重置名单移除会话",
            "",
            "8️⃣ 内存阈值",
            "   用法：内存阈值 <数值[单位]> / 开 / 关 / 状态",
            "   示例：内存阈值 85        （按默认单位，默认%）",
            "   示例：内存阈值 2048 mb   （带单位，自动切换为MB）",
            "   示例：内存阈值 状态      （查看当前设置）",
            "   说明：只输数字按默认单位，带单位后缀优先",
            "",
            "9️⃣ 内存单位",
            "   用法：内存单位 <percent|mb> / 状态",
            "   示例：内存单位 mb     （默认单位切为MB）",
            "   示例：内存单位 状态  （查看当前默认单位）",
            "   说明：只输数字时按该默认单位解释",
            "",
            "🔟 内存监控",
            "   用法：内存监控 <数值[单位]> / 开 / 关 / 状态",
            "   示例：内存监控 80          （设阈值并自动开启监控）",
            "   示例：内存监控 2048 mb     （MB单位同款用法）",
            "   说明：设阈值+开监控一条指令搞定",
            "",
            "1️⃣1️⃣ 内存检查间隔",
            "   用法：内存检查间隔 <秒数> / 默认",
            "   示例：内存检查间隔 60  （每60秒检查一次）",
            "   示例：内存检查间隔 默认  （恢复30秒）",
            "",
            "1️⃣2️⃣ 内存冷却",
            "   用法：内存冷却 <秒数> / 默认",
            "   示例：内存冷却 300  （触发重启后冷却5分钟）",
            "   示例：内存冷却 默认  （恢复600秒）",
            "",
            "1️⃣3️⃣ 重置自动重启",
            "   用法：重置自动重启",
            "   说明：重置自动重启倒计时并重载内存监控配置",
            "",
            "1️⃣4️⃣ 重载",
            "   用法：重载 <插件名/序号>",
            "   示例：重载 1       （重载序号1的插件）",
            "   示例：重载 all     （重载所有插件）",
            "   说明：空参时列出所有插件及序号",
            "",
            "1️⃣5️⃣ 插件状态",
            "   用法：插件状态",
            "   说明：查看重启插件当前配置状态",
            "",
            "1️⃣6️⃣ 性能",
            "   用法：性能",
            "   说明：查看系统 CPU 和内存占用",
            "",
        ]
        yield event.plain_result("\n".join(lines))
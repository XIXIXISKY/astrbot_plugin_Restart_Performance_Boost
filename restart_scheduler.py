# restart_scheduler.py
import asyncio
import json
import time
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .dashboard_client import DashboardClient
from .utils import fmt_seconds, is_memory_over_threshold, persist_restart_cache


class RestartScheduler:
    """
    统一的重启调度器
    - 间隔循环重启（由 auto_restart_interval_seconds 控制，单位秒）
    - 内存阈值监控重启（由 memory_monitor_* 系列配置控制）
    - 0 或负数表示关闭
    """

    def __init__(
        self, context: Context, config: AstrBotConfig, dashboard: DashboardClient, cache: dict | None = None
    ):
        self.context = context
        self.config = config
        self.dashboard = dashboard
        self.cache = cache or {}

        self._interval_task: asyncio.Task | None = None
        self._interval_running = False

        # 内存监控
        self._memory_task: asyncio.Task | None = None
        self._memory_running = False
        self._last_memory_restart_time: float = 0

        # 每日定时重置上下文
        self._ctx_reset_task: asyncio.Task | None = None
        self._ctx_reset_running = False
        self._last_ctx_reset_date: str = self.cache.get("_last_ctx_reset_date", "")

    # ================== 生命周期 ==================

    async def start(self):
        """根据配置启动间隔循环和内存监控；配置无效则关闭"""
        await self.start_interval()
        await self.start_memory_monitor()
        await self.start_ctx_reset()

    async def shutdown(self):
        """停止所有定时任务"""
        await self.stop_interval()
        await self.stop_memory_monitor()
        await self.stop_ctx_reset()

    # ================== 间隔循环重启 ==================

    def _get_interval(self) -> float:
        """读取配置中的自动重启间隔（秒），无效或<=0返回0表示关闭"""
        try:
            val = float(self.config.get("auto_restart_interval_seconds", 0))
        except (TypeError, ValueError):
            val = 0
        return val if val > 0 else 0

    async def start_interval(self):
        """根据配置启动间隔循环；若配置无效则关闭。"""
        await self.stop_interval()
        interval = self._get_interval()
        if interval <= 0:
            logger.debug("[重启插件] 自动重启未开启（间隔<=0）")
            return
        self._interval_running = True
        self._interval_task = asyncio.create_task(self._interval_loop(interval))

    async def stop_interval(self):
        """停止间隔循环"""
        self._interval_running = False
        if self._interval_task:
            self._interval_task.cancel()
            try:
                await self._interval_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[重启插件] 停止间隔循环时出错：{e}")
            self._interval_task = None

    async def reload_interval(self):
        """热重载间隔配置：取消当前倒计时，用最新配置重新开始。"""
        old_interval = self._get_interval()
        was_running = self._interval_running
        await self.stop_interval()
        if was_running and old_interval > 0:
            logger.info(
                f"[重启插件] 配置变更，倒计时已取消，重新以 {fmt_seconds(old_interval)} 开始"
            )
        await self.start_interval()

    async def _interval_loop(self, interval: float):
        """间隔循环主体：倒计时 → 重启 → 重新读取配置 → 继续循环"""
        logger.info(f"[重启插件] 自动重启开启，间隔 {fmt_seconds(interval)}")
        while self._interval_running:
            try:
                remaining = interval
                # 倒计时
                while remaining > 0 and self._interval_running:
                    sleep_for = min(remaining, 60)  # 每60秒检查一次
                    await asyncio.sleep(sleep_for)
                    if not self._interval_running:
                        break
                    remaining -= sleep_for
                    if remaining > 0:
                        logger.debug(
                            f"[重启插件] 距离下次重启还有 {fmt_seconds(remaining)}"
                        )

                if not self._interval_running:
                    break

                # 倒计时结束，执行重启
                logger.info(
                    f"[重启插件] 倒计时结束（{fmt_seconds(interval)}），开始执行重启…"
                )
                try:
                    await self.restart(reason="定时重启")
                except Exception as e:
                    logger.error(f"[重启插件] 重启执行失败：{e}")
                    # 出错后短暂等待再继续，避免死循环刷日志
                    await asyncio.sleep(10)

                # 重启后（如果进程还在）重新读取配置，继续循环
                interval = self._get_interval()
                if interval <= 0:
                    logger.info("[重启插件] 自动重启已关闭")
                    break
                logger.info(
                    f"[重启插件] 重启完成，重新开始 {fmt_seconds(interval)} 倒计时"
                )
            except asyncio.CancelledError:
                logger.info("[重启插件] 间隔循环被取消")
                break
            except Exception as e:
                logger.error(f"[重启插件] 间隔循环异常，10秒后自动恢复：{e}")
                await asyncio.sleep(10)
                # 重新读取配置，继续循环
                interval = self._get_interval()
                if interval <= 0:
                    break

    # ================== 动作 ==================

    def _resolve_notify_platform(self, notify_umo: str, configured: str) -> str:
        """方案B：notify_platform 为 auto/空时，自动检测当前实际平台。

        优先从 UMO 前缀解析平台名；否则从当前运行的平台实例里挑一个。
        """
        configured = (configured or "").strip()
        if configured and configured != "auto":
            return configured
        if notify_umo:
            try:
                head = str(notify_umo).split(":", 1)[0].strip()
                if head:
                    return head
            except Exception:
                pass
        try:
            insts = getattr(getattr(self.context, "platform_manager", None), "platform_insts", None) or []
            for inst in insts:
                try:
                    mid = inst.meta().id
                    if mid:
                        return mid
                except Exception:
                    continue
        except Exception:
            pass
        return configured

    async def restart(self, reason: str = "手动重启", trigger_mem: str | None = None):
        """调用 Dashboard 接口执行重启，并更新缓存时间戳和原因以便发通知"""
        # 优先使用缓存里的平台信息和会话；否则回落到 notify_umo/notify_platform 配置，未配置则为空（跳过通知）
        if self.cache.get("umo") and self.cache.get("platform_id"):
            notify_umo = self.cache["umo"]
            notify_platform = self.cache["platform_id"]
        else:
            notify_umo = self.config.get("notify_umo", "")
            configured_platform = self.config.get("notify_platform", "")
            notify_platform = self._resolve_notify_platform(notify_umo, configured_platform)
            if not notify_umo:
                notify_umo = ""
                notify_platform = ""
        self.cache["start_ts"] = time.time()
        self.cache["restart_reason"] = reason
        self.cache["umo"] = notify_umo
        self.cache["platform_id"] = notify_platform
        # 记录触发重启时的内存占用（发送重启后反馈用）
        if trigger_mem:
            self.cache["trigger_mem"] = trigger_mem
        else:
            try:
                from .utils import get_memory_usage_percent
                self.cache["trigger_mem"] = "{:.1f}%".format(get_memory_usage_percent())
            except Exception:
                self.cache["trigger_mem"] = "未知"
        # 发送重启提示
        try:
            await self.context.send_message(
                session=notify_umo,
                message_chain=MessageChain([Plain("正在重启 AstrBot…")]),
            )
        except Exception as e:
            logger.warning(f"[重启插件] 发送重启提示失败：{e}")
        # 从文件读取当前间隔值，避免覆盖用户已修改的"关"指令
        try:
            with open(self.config.config_path, "r", encoding="utf-8-sig") as f:
                file_data = json.load(f)
            self.config["auto_restart_interval_seconds"] = file_data.get(
                "auto_restart_interval_seconds", 0
            )
        except Exception:
            pass
        self.config.save_config()
        await asyncio.sleep(2)
        await self.dashboard.restart()

    # ================== 内存阈值监控重启 ==================

    def _get_memory_config(self) -> dict:
        """读取内存监控相关配置"""
        return {
            "enabled": bool(self.config.get("memory_monitor_enabled", False)),
            "threshold_value": self.config.get("memory_threshold_value", 80),
            "threshold_unit": str(self.config.get("memory_threshold_unit", "percent")),
            "check_interval": max(float(self.config.get("memory_check_interval", 30)), 5),
            "cooldown_seconds": max(float(self.config.get("memory_cooldown_seconds", 600)), 0),
        }

    async def start_memory_monitor(self):
        """根据配置启动内存监控循环；未开启则关闭"""
        await self.stop_memory_monitor()
        mconf = self._get_memory_config()
        if not mconf["enabled"]:
            logger.debug("[重启插件] 内存监控未开启")
            return
        self._memory_running = True
        self._memory_task = asyncio.create_task(self._memory_monitor_loop(mconf))
        logger.info(
            f"[重启插件] 内存监控已开启：阈值 {mconf['threshold_value']}{mconf['threshold_unit']}，"
            f"每 {int(mconf['check_interval'])} 秒检查一次，冷却 {int(mconf['cooldown_seconds'])} 秒"
        )

    async def stop_memory_monitor(self):
        """停止内存监控循环"""
        self._memory_running = False
        if self._memory_task:
            self._memory_task.cancel()
            try:
                await self._memory_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[重启插件] 停止内存监控时出错：{e}")
            self._memory_task = None

    async def reload_memory_monitor(self):
        """热重载内存监控配置：取消当前循环，用最新配置重新开始。"""
        was_running = self._memory_running
        await self.stop_memory_monitor()
        if was_running:
            logger.info("[重启插件] 内存监控配置变更，重新读取并启动")
        await self.start_memory_monitor()

    async def _memory_monitor_loop(self, mconf: dict):
        """内存监控循环主体：定期检查 → 超阈值时触发重启"""
        logger.info(
            f"[重启插件] 内存监控循环启动（阈值 {mconf['threshold_value']}{mconf['threshold_unit']}）"
        )
        while self._memory_running:
            await asyncio.sleep(mconf["check_interval"])
            if not self._memory_running:
                break

            try:
                over, current = is_memory_over_threshold(
                    mconf["threshold_value"], mconf["threshold_unit"]
                )
            except Exception as e:
                logger.error(f"[重启插件] 读取内存信息失败：{e}")
                continue

            logger.debug(
                f"[重启插件] 内存巡检：当前 {current:.1f}{mconf['threshold_unit']}，"
                f"阈值 {mconf['threshold_value']}{mconf['threshold_unit']}"
            )

            if not over:
                continue

            # 超过阈值，检查冷却期
            now = time.time()
            remaining_cooldown = mconf["cooldown_seconds"] - (now - self._last_memory_restart_time)
            if remaining_cooldown > 0:
                logger.warning(
                    f"[重启插件] 内存超阈值（{current:.1f}{mconf['threshold_unit']}），"
                    f"但处于冷却期，跳过重启（剩余 {fmt_seconds(remaining_cooldown)}）"
                )
                continue

            # 触发重启
            logger.warning(
                f"[重启插件] 内存超阈值（{current:.1f}{mconf['threshold_unit']} > "
                f"{mconf['threshold_value']}{mconf['threshold_unit']}），触发重启…"
            )
            try:
                # 更新冷却时间戳
                self._last_memory_restart_time = now
                # 触发时内存按阈值单位格式化（MB 阈值显示 MB，percent 阈值显示 %）
                unit = str(mconf["threshold_unit"]).strip().lower()
                trigger_mem = (
                    "{:.1f}MB".format(current)
                    if unit == "mb"
                    else "{:.1f}%".format(current)
                )
                await self.restart(
                    reason="内存超阈值",
                    trigger_mem=trigger_mem,
                )
            except Exception as e:
                logger.error(f"[重启插件] 内存触发重启执行失败：{e}")
                # 出错后短暂等待再继续，避免刷日志
                await asyncio.sleep(10)

            # 重启后（进程还在则继续）重新读取配置
            mconf = self._get_memory_config()
            if not mconf["enabled"]:
                logger.info("[重启插件] 内存监控已关闭")
                break

    # ================== 每日定时重置上下文 ==================

    def _get_ctx_reset_time(self) -> str:
        """读取每日定时重置上下文的时间配置（HH:MM），无效返回空串表示关闭"""
        val = str(self.config.get("ctx_reset_time", "") or "").strip()
        if not val:
            return ""
        # 校验 HH:MM 24小时制格式
        try:
            datetime.strptime(val, "%H:%M")
        except ValueError:
            logger.warning(f"[重启插件] 定时重置时间格式无效：{val!r}，已忽略")
            return ""
        return val

    async def start_ctx_reset(self):
        """根据配置启动每日定时重置上下文循环；未配置则关闭"""
        await self.stop_ctx_reset()
        reset_time = self._get_ctx_reset_time()
        if not reset_time:
            logger.debug("[重启插件] 每日定时重置上下文未开启")
            return
        if (not self.cache.get("ctx_reset_umo") and not self.cache.get("ctx_reset_private_umo")) or not self.cache.get("ctx_reset_platform_id"):
            logger.warning("[重启插件] 定时重置上下文目标会话未记录，环循不启动。请先设置：定时重置 <HH:MM>")
            return
        self._ctx_reset_running = True
        self._ctx_reset_task = asyncio.create_task(self._ctx_reset_loop(reset_time))
        logger.info(f"[重启插件] 每日定时重置上下文已开启：每天 {reset_time} 触发")

    async def stop_ctx_reset(self):
        """停止每日定时重置上下文循环"""
        self._ctx_reset_running = False
        if self._ctx_reset_task:
            self._ctx_reset_task.cancel()
            try:
                await self._ctx_reset_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[重启插件] 停止定时重置上下文时出错：{e}")
            self._ctx_reset_task = None

    async def reload_ctx_reset(self):
        """热重载定时重置配置：取消当前循环，用最新配置重新开始"""
        was_running = self._ctx_reset_running
        await self.stop_ctx_reset()
        if was_running:
            logger.info("[重启插件] 定时重置上下文配置变更，重新读取并启动")
        await self.start_ctx_reset()

    async def _ctx_reset_loop(self, reset_time: str):
        """每日定时重置上下文循环主体：到点触发reset，直接清空登记的目标会话上下文"""
        logger.info(f"[重启插件] 定时重置上下文循环启动（每天 {reset_time}）")
        while self._ctx_reset_running:
            now = datetime.now()
            target = datetime.strptime(reset_time, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
            today = now.strftime("%Y-%m-%d")
            if self._last_ctx_reset_date == today:
                if now >= target:
                    # 今天已执行过且当前已过目标时间，跳到明天
                    tomorrow_target = target + timedelta(days=1)
                    delta = (tomorrow_target - now).total_seconds()
                    await asyncio.sleep(min(delta, 60))
                    continue
                else:
                    # 当前还没到目标时间，标记是旧数据（重启后残留），清除重新等待
                    self._last_ctx_reset_date = ""
                    self.cache["_last_ctx_reset_date"] = ""
                    self.config.save_config()
                    logger.info("[重启插件] 清除上次重置日期标记，重新等待今日定时重置")
            if not self._last_ctx_reset_date:
                # 首次启动
                if now >= target:
                    # 已过点，立即触发重置
                    logger.info(f"[重启插件] 首次启动已过目标时间 {reset_time}，立即触发reset")
                else:
                    # 还没到点，等到目标时间
                    delta = (target - now).total_seconds()
                    await asyncio.sleep(delta)
                    continue
            if now < target:
                # 还没到点，等到目标时间
                delta = (target - now).total_seconds()
                await asyncio.sleep(min(delta, 60))
                continue
            # 已到点或刚过点，触发重置
            logger.info(f"[重启插件] 到达每日重置时间 {reset_time}，触发reset")
            self._last_ctx_reset_date = today
            self.cache["_last_ctx_reset_date"] = today
            self.config.save_config()
            try:
                await self._trigger_reset_command()
            except Exception as e:
                logger.error(f"[重启插件] 触发reset失败：{e}")
            # 执行完再等下一轮
            await asyncio.sleep(60)

    async def _trigger_reset_command(self):
        """触发reset：直接清空登记的目标会话上下文（支持群聊和私聊，不经唤醒词）"""
        import json
        platform_id = self.cache.get("ctx_reset_platform_id")
        if not platform_id:
            logger.warning("[重启插件] 定时重置未记录平台信息，跳过")
            return

        from astrbot.core.utils.active_event_registry import active_event_registry

        # 收集所有目标UMO（群聊 + 私聊）
        group_umos = []
        private_umos = []

        # 群聊
        raw_umo = self.cache.get("ctx_reset_umo")
        if raw_umo:
            try:
                umo_list = json.loads(raw_umo)
                if isinstance(umo_list, str):
                    umo_list = [umo_list]
            except (json.JSONDecodeError, TypeError):
                umo_list = [raw_umo]
            group_umos.extend([u for u in umo_list if u])

        # 私聊
        raw_private = self.cache.get("ctx_reset_private_umo")
        if raw_private:
            try:
                p_list = json.loads(raw_private)
                if isinstance(p_list, str):
                    p_list = [p_list]
            except (json.JSONDecodeError, TypeError):
                p_list = [raw_private]
            private_umos.extend([u for u in p_list if u])

        all_umos = group_umos + private_umos
        if not all_umos:
            logger.warning("[重启插件] 定时重置未记录目标会话，跳过")
            return

        clear_count = 0
        for umo in all_umos:
            if not umo:
                continue
            try:
                active_event_registry.stop_all(umo)
            except Exception:
                pass
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if cid:
                await self.context.conversation_manager.update_conversation(umo, cid, [])
                logger.info(f"[重启插件] 已清空会话上下文：{umo}")
            else:
                logger.info(f"[重启插件] 会话尚无上下文，无需清空：{umo}")
            clear_count += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        total = len(all_umos)
        from .utils import format_clear_summary
        summary = format_clear_summary(clear_count, total, group_umos, private_umos, now_str)
        logger.info(f"[重启插件] 已一键清空 {clear_count}/{total} 个会话上下文（清除时间：{now_str}）")
        # 反馈发给用户配置的通知目标；未配置则跳过
        notify_umo = self.config.get("notify_umo", "")
        try:
            if notify_umo:
                await self.context.send_message(
                    session=notify_umo,
                    message_chain=MessageChain([Plain(summary)]),
                )
        except Exception as e:
            logger.warning(f"[重启插件] 发送反馈通知失败 {notify_umo}: {e}")
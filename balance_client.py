# balance_client.py
# 大模型平台余额查询客户端（供"余额"命令使用）
# 数据源为 AstrBot 已配置的 Provider（api_base + key）。

import asyncio
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from astrbot.api import logger

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# NEW API（new-api）额度换算：1 美元 = 500000 quota
NEWAPI_QUOTA_PER_USD = 500000

def _strip_v1(api_base: str) -> str:
    """去掉 OpenAI 兼容端点里的 /v1（或 /v1/xxx）后缀，返回站点根地址。"""
    b = (api_base or "").strip().rstrip("/")
    if "/v1" in b:
        return b.split("/v1")[0]
    return b


@dataclass
class BalanceResult:
    """统一的余额查询结果。"""
    source_name: str = ""
    currency: str = ""
    total_balance: str = "0"
    used_balance: str = "0"
    remaining_balance: str = "0"
    raw_info: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def render(self) -> str:
        """渲染为单条文本（成功/失败）。"""
        if self.error:
            return f"🔴 {self.source_name}：{self.error}"
        if self.remaining_balance == self.total_balance:
            head = f"🟢 {self.source_name}：{self.total_balance} {self.currency}"
            if self.used_balance not in ("", "0"):
                head += f" / 已用 {self.used_balance} {self.currency}"
            head = head.rstrip()
        else:
            head = (
                f"🟢 {self.source_name}：余额 {self.remaining_balance} {self.currency}"
                f" / 总额 {self.total_balance} {self.currency}"
            )
            if self.used_balance != "0":
                head += f" / 已用 {self.used_balance} {self.currency}"
        if self.raw_info:
            head += f"（{self.raw_info}）"
        return head.rstrip()

    def render_tpl(
        self,
        success_template: str = "",
        error_template: str = "",
        api_key: str = "",
    ) -> str:
        """按模板渲染（成功/失败分开模板）；空模板回退到 render()。

        支持变量（均为双层大括号）：
          {{source_name}} {{currency}} {{balance}} {{total_balance}}
          {{remaining_balance}} {{used_balance}} {{raw_info}} {{api_key}}
          {{smart_balance}} 附加信息块（总额/已用/备注，自动跳过无意义行）
        条件行：{{?变量}} 值为空或"0"时移除整行；模板内 \n 会被转成换行。
        """
        if self.error:
            if not error_template:
                error_template = "🔴 **{{source_name}}**\n  ❌ {{error}}"
            return self._render_error_tpl(error_template, api_key)
        if not success_template:
            return self.render()
        return self._render_success_tpl(success_template, api_key)

    def _render_error_tpl(self, template: str, api_key: str) -> str:
        replacements = {
            "{{api_key}}": api_key,
            "{{source_name}}": self.source_name,
            "{{error}}": self.error,
        }
        result = template
        for key, value in replacements.items():
            result = result.replace(key, str(value))
        return result.replace("\\n", "\n")

    def _render_success_tpl(self, template: str, api_key: str) -> str:
        indent = "  "
        parts = []
        if self.remaining_balance != self.total_balance:
            parts.append(f"{indent}📈 总额: {self.total_balance} {self.currency}")
            if self.used_balance != "0":
                parts.append(f"{indent}📊 已用: {self.used_balance} {self.currency}")
        if self.raw_info:
            parts.append(f"{indent}📝 {self.raw_info}")
        smart_balance = "\n".join(parts) if parts else ""
        template = template.replace("{{smart_balance}}", smart_balance)

        smart_val = (
            self.remaining_balance
            if self.remaining_balance != self.total_balance
            else self.total_balance
        )
        replacements = {
            "{{api_key}}": api_key,
            "{{source_name}}": self.source_name,
            "{{currency}}": self.currency,
            "{{balance}}": smart_val,
            "{{total_balance}}": self.total_balance,
            "{{remaining_balance}}": self.remaining_balance,
            "{{used_balance}}": self.used_balance,
            "{{raw_info}}": self.raw_info,
        }
        # 条件行 {{?变量}}：值为空或"0"时移除整行
        for key, value in replacements.items():
            cond_key = key.replace("{{", "{{?")
            if cond_key in template:
                if not str(value).strip() or str(value).strip() == "0":
                    for line in template.split("\n"):
                        if cond_key in line:
                            template = template.replace(
                                line + "\n", ""
                            ).replace("\n" + line, "").replace(line, "")
        result = template
        for key, value in replacements.items():
            result = result.replace(key, str(value))
            # 条件行标记 {{?变量}} 在值非空时替换为空（它只是占位标记）
            result = result.replace(key.replace("{{", "{{?"), "")
        return result.replace("\\n", "\n")


class BaseBalanceFetcher(ABC):
    """余额查询基类。"""

    # 平台别名（小写），用于命令 /余额 <平台名> 模糊匹配
    aliases: list[str] = []

    @abstractmethod
    def match(self, api_base: str) -> bool:
        """判断该 fetcher 是否支持此 api_base。"""

    def match_by_key(self, api_key: str) -> bool:
        """判断该 fetcher 是否支持此 api_key（api_base 匹配失败时的兜底）。"""
        return False

    @abstractmethod
    async def fetch(
        self, session: aiohttp.ClientSession, api_key: str, api_base: str
    ) -> BalanceResult:
        """执行查询。"""

    def match_by_name(self, name: str) -> bool:
        """根据平台名/别名模糊匹配。"""
        name_lower = name.lower().strip()
        if not name_lower:
            return False
        return any(name_lower in a or a in name_lower for a in self.aliases)


class DeepSeekFetcher(BaseBalanceFetcher):
    aliases = ["deepseek", "ds", "深度求索"]

    def match(self, api_base: str) -> bool:
        return "deepseek" in api_base.lower()

    def match_by_key(self, api_key: str) -> bool:
        # DeepSeek 官方 API 固定使用 api.deepseek.com
        # 当 api_base 不匹配但 key 是 sk- 开头时兜底尝试
        return api_key.startswith("sk-")

    async def fetch(self, session, api_key, api_base) -> BalanceResult:
        url = "https://api.deepseek.com/user/balance"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return BalanceResult("DeepSeek", error=f"HTTP {resp.status}")
                data = await resp.json()
        except Exception as e:
            return BalanceResult("DeepSeek", error=f"请求异常: {e}")

        if not data.get("is_available"):
            return BalanceResult("DeepSeek", error="账户不可用（欠费/封禁）")
        infos = data.get("balance_infos") or []
        if not infos:
            return BalanceResult("DeepSeek", error="未找到余额信息")
        info = infos[0]
        total = str(info.get("total_balance", "0"))
        granted = str(info.get("granted_balance", "0"))
        topped = str(info.get("topped_up_balance", "0"))
        return BalanceResult(
            source_name="DeepSeek",
            currency=info.get("currency", "CNY"),
            total_balance=total,
            remaining_balance=total,
            raw_info=f"赠送 {granted} / 充值 {topped}",
        )


class SiliconFlowFetcher(BaseBalanceFetcher):
    aliases = ["siliconflow", "siliconcloud", "硅基", "硅基流动", "sc"]

    def match(self, api_base: str) -> bool:
        return "siliconflow" in api_base.lower() or "硅基" in api_base

    async def fetch(self, session, api_key, api_base) -> BalanceResult:
        domain = "api.siliconflow.cn" if "siliconflow.cn" in api_base else "api.siliconflow.com"
        url = f"https://{domain}/v1/user/info"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return BalanceResult("硅基流动", error=f"HTTP {resp.status}")
                data = await resp.json()
        except Exception as e:
            return BalanceResult("硅基流动", error=f"请求异常: {e}")
        if data.get("code") != 20000:
            return BalanceResult("硅基流动", error=f"API: {data.get('message')}")
        inner = data.get("data") or {}
        total = inner.get("totalBalance") or inner.get("balance") or "0"
        charge = inner.get("chargeBalance", "0")
        return BalanceResult(
            source_name="硅基流动",
            currency="USD",
            total_balance=str(total),
            remaining_balance=str(total),
            raw_info=f"充值 {charge}",
        )


class MoonshotFetcher(BaseBalanceFetcher):
    aliases = ["moonshot", "kimi", "月之暗面"]

    def match(self, api_base: str) -> bool:
        return "moonshot" in api_base.lower()

    async def fetch(self, session, api_key, api_base) -> BalanceResult:
        url = "https://api.moonshot.cn/v1/users/me/balance"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return BalanceResult("Moonshot", error=f"HTTP {resp.status}")
                data = await resp.json()
        except Exception as e:
            return BalanceResult("Moonshot", error=f"请求异常: {e}")
        if data.get("code") != 0 or not data.get("status"):
            return BalanceResult("Moonshot", error=f"API: {data}")
        avail = data.get("data", {}).get("available_balance", 0)
        return BalanceResult(
            source_name="Moonshot(Kimi)",
            currency="CNY",
            total_balance=str(avail),
            remaining_balance=str(avail),
        )


class OpenAIFetcher(BaseBalanceFetcher):
    aliases = ["openai", "gpt", "chatgpt"]

    def match(self, api_base: str) -> bool:
        return "openai.com" in api_base.lower()

    async def fetch(self, session, api_key, api_base) -> BalanceResult:
        base = "https://api.openai.com"
        if api_base and "openai.com" in api_base:
            base = api_base.rstrip("/")
            if "/v1" in base:
                base = base.split("/v1")[0]
        headers = {"Authorization": f"Bearer {api_key}"}
        today = datetime.today().strftime("%Y-%m-%d")

        account_balance = 0.0
        has_payment = False
        access_until = "无限制"
        try:
            async with session.get(
                f"{base}/v1/dashboard/billing/subscription",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    sub = await resp.json()
                    if isinstance(sub, list) and sub:
                        account_balance = sub[0].get("soft_limit_usd", 0)
                        has_payment = sub[0].get("has_payment_method", False)
                        access_until = sub[0].get("access_until", "无限制")
        except Exception:
            pass

        used = 0.0
        try:
            async with session.get(
                f"{base}/v1/dashboard/billing/usage?start_date={today}&end_date={today}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    usage = await resp.json()
                    used = usage.get("total_usage", 0) / 100
        except Exception:
            pass

        if account_balance == 0 and used == 0:
            return BalanceResult("OpenAI", error="无法获取余额（API 不支持或为空）")
        remaining = account_balance - used
        return BalanceResult(
            source_name="OpenAI",
            currency="USD",
            total_balance=f"{account_balance:.2f}",
            remaining_balance=f"{remaining:.2f}",
            used_balance=f"{used:.2f}",
            raw_info=f"支付{'是' if has_payment else '否'} / 到期 {access_until}",
        )


class NewApiFetcher(BaseBalanceFetcher):
    """NEW API（new-api 中转站），通过 /api/usage/token 查询当前 key 额度。

    该接口无固定域名，由 BalanceManager 传入匹配 URL。
    """

    aliases = ["newapi", "new api", "new_api", "中转"]

    def match(self, api_base: str) -> bool:
        # 有固定域名特征时才命中，其余交由 Manager 用 newapi_urls 匹配
        return False

    async def fetch(self, session, api_key, api_base) -> BalanceResult:
        if not api_base:
            return BalanceResult("NEW API", error="未配置站点地址")
        url = _strip_v1(api_base).rstrip("/") + "/api/usage/token"
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    return BalanceResult("NEW API", error=f"非JSON(HTTP {resp.status}): {text[:120]}")
        except Exception as e:
            return BalanceResult("NEW API", error=f"请求异常: {e}")

        ok_flag = bool(data.get("code") or data.get("success"))
        if not ok_flag or "data" not in data:
            err = data.get("message") or f"HTTP 状态异常"
            return BalanceResult("NEW API", error=f"{err}")
        d = data.get("data") or {}
        granted = d.get("total_granted", 0)
        used = d.get("total_used", 0)
        available = d.get("total_available", 0)
        unlimited = d.get("unlimited_quota", False)
        expires_at = d.get("expires_at", 0)
        expires = "永不过期" if not expires_at else datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")

        if unlimited:
            remains = "无限"
        else:
            usd = available / NEWAPI_QUOTA_PER_USD
            remains = f"{available}（约 {usd:.2f} USD）"

        return BalanceResult(
            source_name="NEW API",
            currency="quota",
            total_balance=str(granted),
            used_balance=str(used),
            remaining_balance=str(available),
            raw_info=f"剩余 {remains} / 到期 {expires}",
        )


class NewApiAccountFetcher(BaseBalanceFetcher):
    """NEW API 账户余额（/api/user/self）。

    与 key 级限额（/api/usage/token）不同，这里查的是账户本身的剩余额度。
    需要「账户 access token」（非 sk- 模型 key），可选带 user id。
    站点 /api/user/self 返回的 quota 即当前余额，无需再减 used_quota。
    """

    aliases = ["newapi账户", "账户余额", "newapi-account", "account", "账号"]

    def match(self, api_base: str) -> bool:
        return False  # 由命令显式调用

    async def fetch(
        self,
        session: aiohttp.ClientSession,
        api_base: str,
        access_token: str,
        user_id: str | int | None = None,
    ) -> BalanceResult:
        if not api_base:
            return BalanceResult("NEW API 账户", error="未配置站点地址")
        if not access_token:
            return BalanceResult("NEW API 账户", error="未配置访问令牌(access token)")
        url = _strip_v1(api_base).rstrip("/") + "/api/user/self"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if user_id not in (None, "", "0"):
            headers["New-API-User"] = str(user_id)
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    return BalanceResult("NEW API 账户", error=f"非JSON(HTTP {resp.status}): {text[:120]}")
                if resp.status != 200:
                    err = data.get("message") or f"HTTP {resp.status}"
                    return BalanceResult("NEW API 账户", error=f"{err}")
        except Exception as e:
            return BalanceResult("NEW API 账户", error=f"请求异常: {e}")

        ok_flag = bool(data.get("success") or data.get("code"))
        if not ok_flag:
            err = data.get("message") or "接口返回异常"
            return BalanceResult("NEW API 账户", error=f"{err}")
        d = data.get("data") or {}
        quota = d.get("quota", 0) or 0
        used_quota = d.get("used_quota", 0) or 0
        username = d.get("username") or d.get("display_name") or ""
        uid = d.get("id", user_id)
        usd = quota / NEWAPI_QUOTA_PER_USD
        used_usd = used_quota / NEWAPI_QUOTA_PER_USD
        tag = f"({uid})" if uid not in (None, "", 0) else ""
        return BalanceResult(
            source_name=f"NEW API 账户{tag}",
            currency="USD",
            total_balance=f"{usd:.4f}",
            used_balance=f"{used_usd:.4f}",
            remaining_balance=f"{usd:.4f}",
            raw_info=(f"用户 {username} · " if username else "") + "quota 即当前余额(4位)",
        )


class NewApiSubscriptionFetcher(BaseBalanceFetcher):
    """NEW API 订阅查询（/api/user/subscription）。

    ⚠ 实验性功能：仅供站点的管理员试用，普通用户请勿使用，效果未知。
    该接口在部分站点需要管理员权限，普通 access token 可能返回 403。
    订阅额度与账户余额是两套独立额度；返回字段若为超大值(quota)会自动换算成美元。
    """

    aliases = ["newapi订阅", "订阅", "subscription"]

    def match(self, api_base: str) -> bool:
        return False  # 由命令显式调用（实验性高级模式）

    async def fetch(
        self,
        session: aiohttp.ClientSession,
        api_base: str,
        access_token: str,
        user_id: str | int | None = None,
    ) -> BalanceResult:
        if not api_base:
            return BalanceResult("NEW API 订阅", error="未配置站点地址")
        if not access_token:
            return BalanceResult("NEW API 订阅", error="未配置访问令牌(access token)")
        url = _strip_v1(api_base).rstrip("/") + "/api/user/subscription"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if user_id not in (None, "", "0"):
            headers["New-API-User"] = str(user_id)
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    return BalanceResult("NEW API 订阅", error=f"非JSON(HTTP {resp.status}): {text[:120]}")
                if resp.status != 200:
                    err = data.get("message") or f"HTTP {resp.status}"
                    return BalanceResult("NEW API 订阅", error=f"{err}（可能需管理员权限）")
        except Exception as e:
            return BalanceResult("NEW API 订阅", error=f"请求异常: {e}")

        ok_flag = bool(data.get("success") or data.get("code"))
        if not ok_flag:
            err = data.get("message") or "接口返回异常"
            return BalanceResult("NEW API 订阅", error=f"{err}")
        d = data.get("data") or {}

        def _to_usd(v):
            """订阅接口多数直接给美元金额；值过大(≥5000)则视为 quota 换算。"""
            try:
                v = float(v or 0)
            except Exception:
                return "0"
            if v >= NEWAPI_QUOTA_PER_USD / 100:
                return f"{v / NEWAPI_QUOTA_PER_USD:.4f}"
            return f"{v:.4f}"

        name = d.get("name") or d.get("plan") or "订阅"
        expire = d.get("expire_at", 0) or 0
        expires = "永不过期" if not expire else datetime.fromtimestamp(expire).strftime("%Y-%m-%d")
        total = _to_usd(d.get("total_quota"))
        used = _to_usd(d.get("used_quota"))
        remain = _to_usd(d.get("remain_quota"))
        raw_parts = []
        for k in ("type", "plan", "cycle_type", "reset_cycle", "id"):
            if k in d and d[k] not in (None, ""):
                raw_parts.append(f"{k}={d[k]}")
        return BalanceResult(
            source_name=f"NEW API 订阅({name})",
            currency="USD",
            total_balance=total,
            used_balance=used,
            remaining_balance=remain,
            raw_info=f"到期 {expires}" + (f" · {' '.join(raw_parts)}" if raw_parts else ""),
        )


class RelayOpenAIFetcher(BaseBalanceFetcher):
    """中转站但走 OpenAI 兼容 billing 接口（/v1/dashboard/billing/*）。"""

    aliases = ["中转", "relay", "兼容"]

    def match(self, api_base: str) -> bool:
        return False  # 作为兜底探测，由 Manager 手动调用

    async def fetch(self, session, api_key, api_base) -> BalanceResult:
        origin = _strip_v1(api_base)
        if not origin:
            return BalanceResult("中转站", error="未配置站点地址")
        headers = {"Authorization": f"Bearer {api_key}"}
        total = 0.0
        has_payment = False
        try:
            async with session.get(
                f"{origin}/v1/dashboard/billing/subscription",
                headers=headers, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    sub = await resp.json()
                    total = float(sub.get("soft_limit_usd") or sub.get("hard_limit_usd") or 0)
                    has_payment = bool(sub.get("has_payment_method"))
        except Exception:
            pass
        used = 0.0
        try:
            end = datetime.now()
            start = end - timedelta(days=99)
            async with session.get(
                f"{origin}/v1/dashboard/billing/usage",
                params={"start_date": start.strftime("%Y-%m-%d"), "end_date": end.strftime("%Y-%m-%d")},
                headers=headers, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    u = await resp.json()
                    used = float(u.get("total_usage", 0)) / 100
        except Exception:
            pass
        if total == 0 and used == 0:
            return BalanceResult("中转站", error="无可用余额接口")
        remaining = total - used
        return BalanceResult(
            source_name="中转站",
            currency="USD",
            total_balance=f"{total:.2f}",
            used_balance=f"{used:.2f}",
            remaining_balance=f"{remaining:.2f}",
            raw_info=f"支付{'是' if has_payment else '否'}",
        )


class ChatAnywhereFetcher(BaseBalanceFetcher):
    """ChatAnywhere（chatanywhere）—— OpenAI 兼容 billing 接口。"""

    aliases = ["chatanywhere", "ca", "chatai"]

    def match(self, api_base: str) -> bool:
        return "chatanywhere" in api_base.lower()

    async def fetch(self, session, api_key, api_base) -> BalanceResult:
        base_url = _strip_v1(api_base)
        if not base_url:
            return BalanceResult("ChatAnywhere", error="未配置站点地址")
        headers = {"Authorization": f"Bearer {api_key}"}
        total = 0.0
        try:
            async with session.get(
                f"{base_url}/v1/dashboard/billing/subscription",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    sub = await resp.json()
                    total = float(
                        sub.get("hard_limit_usd") or sub.get("soft_limit_usd") or 0
                    )
        except Exception:
            pass
        used = 0.0
        try:
            end = datetime.now()
            start = end - timedelta(days=99)
            async with session.get(
                f"{base_url}/v1/dashboard/billing/usage",
                params={
                    "start_date": start.strftime("%Y-%m-%d"),
                    "end_date": end.strftime("%Y-%m-%d"),
                },
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    u = await resp.json()
                    used = float(u.get("total_usage", 0)) / 100
        except Exception:
            pass
        if total == 0 and used == 0:
            return BalanceResult("ChatAnywhere", error="无法获取余额信息（API 不支持或返回为空）")
        remaining = total - used
        return BalanceResult(
            source_name="ChatAnywhere",
            currency="USD",
            total_balance=f"{total:.2f}",
            used_balance=f"{used:.2f}",
            remaining_balance=f"{remaining:.2f}",
        )


class BalanceManager:
    """余额查询管理器：按 api_base 路由到对应 Fetcher。"""

    def __init__(self, newapi_urls: list[str] | None = None):
        self.fetchers: list[BaseBalanceFetcher] = [
            DeepSeekFetcher(),
            SiliconFlowFetcher(),
            MoonshotFetcher(),
            OpenAIFetcher(),
            ChatAnywhereFetcher(),
            NewApiFetcher(),
        ]
        self.newapi_urls: list[str] = []
        for url in (newapi_urls or []):
            url = url.strip().rstrip("/")
            if url:
                self.newapi_urls.append(url)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def query(self, api_key: str, api_base: str) -> BalanceResult:
        """按 api_base 自动匹配并查询。"""
        if not api_key:
            return BalanceResult("Unknown", error="未提供 API Key")
        session = await self._get_session()

        # 1. 平台特征匹配
        for fetcher in self.fetchers:
            try:
                if fetcher.match(api_base):
                    return await fetcher.fetch(session, api_key, api_base)
            except Exception:
                continue

        # 1.5. api_base 不匹配时，尝试通过 key 特征匹配
        for fetcher in self.fetchers:
            try:
                if fetcher.match_by_key(api_key):
                    return await fetcher.fetch(session, api_key, api_base)
            except Exception:
                continue

        # 2. NEW API 专用 URL 匹配
        lower = api_base.lower()
        for url in self.newapi_urls:
            if url.lower() in lower:
                try:
                    return await NewApiFetcher().fetch(session, api_key, url)
                except Exception as e:
                    return BalanceResult("NEW API", error=f"请求异常: {e}")

        # 3. 兜底：自动探测中转站（new-api 或 OpenAI 兼容 billing）
        for probe in (NewApiFetcher(), RelayOpenAIFetcher()):
            try:
                result = await probe.fetch(session, api_key, api_base)
                if result.ok:
                    return result
            except Exception:
                continue
        return BalanceResult("Unknown", error=f"暂不支持该 API Base: {api_base}")
# ==================== YAML 自定义服务查询 ====================

class YamlBalanceQueryer:
    """按 YAML 配置查询任意 API 服务的余额。

    配置格式（balance_yaml_config）：
        services:
          aliyun:
            display_name: 阿里云
            url: "https://api.aliyun.com/xxx"
            method: "GET"
            headers:
              Authorization: "Bearer token"
            result_template: "阿里云余额：{{data.balance}}元"
          openai:
            url: "https://api.openai.com/xxx"
            result_template: "OpenAI 余额：${{ {data.balance} }}"

    模板支持：
      - {{data.xxx}}             取 JSON 响应路径（支持 . 和列表下标 [i]）
      - {{ {表达式} }}           数学表达式（可引用 {{data.x}}，支持 % - / * +、abs/round/min/max/pow/sqrt/floor/ceil/log/exp/pi/e）
      - 单层 {path} 会被直接替换为对应值（兼容旧格式）
    """

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def query(self, config_str: str) -> list[str]:
        """解析 YAML 并并发查询所有服务。"""
        if yaml is None:
            return ["YAML 依赖未安装（pip install pyyaml）"]
        if not (config_str or "").strip():
            return ["未配置 balance_yaml_config"]
        try:
            config_data = yaml.safe_load(config_str)
        except Exception as e:
            return [f"YAML 配置解析失败: {e}"]
        services = (config_data or {}).get("services", {}) if isinstance(config_data, dict) else {}
        if not services:
            return ["YAML 中未配置任何 services"]

        session = await self._get_session()
        tasks = [self._query_one(session, name, info) for name, info in services.items()]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[str] = []
        for r in responses:
            if isinstance(r, str):
                results.append(r)
            elif isinstance(r, Exception):
                logger.error(f"YAML 服务查询异常: {r}")
        return results

    async def _query_one(self, session, name: str, info) -> str:
        if not isinstance(info, dict):
            return f"{name}: 配置错误（应为 dict）"
        display_name = info.get("display_name") or name
        url = info.get("url", "")
        method = (info.get("method", "GET") or "GET").upper()
        headers = info.get("headers", {}) or {}
        result_template = info.get("result_template", "{{data}}")

        if not url:
            return f"{display_name}: 缺失 URL"
        try:
            async with session.request(
                method, url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return f"{display_name}: HTTP {resp.status}"
                try:
                    data = await resp.json()
                except Exception:
                    return f"{display_name}: 非 JSON 响应"
                rendered = self._render_template(result_template, data)
                return f"{display_name}:\n{rendered}"
        except asyncio.TimeoutError:
            return f"{display_name}: 请求超时"
        except Exception as e:
            logger.error(f"[{name}] YAML 查询异常: {type(e).__name__}: {e}")
            return f"{display_name}: 异常"

    @classmethod
    def _render_template(cls, template: str, data: dict) -> str:
        """渲染 YAML 服务模板：
        - {{data.xxx}}                路径引用（点号 + 可选 [i] 下标）
        - {{ {表达式} }} / {{ 100 - {a} }}   数学表达式（安全函数）
        - {data.xxx}                  单层大括号兼容
        - {{data}}                    输出整个 JSON
        """
        def _fmt(v):
            if v is None:
                return None
            if isinstance(v, float) and not v.is_integer():
                return f"{v:.2f}"
            return str(v)

        def _path_value(path: str):
            p = path.strip()
            if p == "data":
                try:
                    import json as _json
                    return _json.dumps(data, ensure_ascii=False)
                except Exception:
                    return str(data)
            sub = p[len("data"):].lstrip(".") if p.startswith("data") else p
            return _fmt(cls._get_by_path(data, sub))

        def _get_val(path: str):
            """取值，兼容 data. 前缀；None 返回 None。"""
            p = path.strip()
            if p.startswith("data"):
                p = p[len("data"):].lstrip(".")
            return cls._get_by_path(data, p)

        def _render_block(m):
            inner = m.group(1).strip()
            # 表达式块：内部含单层 {路径}
            if "{" in inner and "}" in inner:
                def _val(m2):
                    v = _get_val(m2.group(1))
                    if v is None:
                        return "0"
                    if isinstance(v, (int, float)):
                        return str(v)
                    cleaned = re.sub(r"[^\d.\-]", "", str(v))
                    return cleaned if cleaned else "0"
                expr = re.sub(r"\{([^{}]+)\}", _val, inner)
                try:
                    return str(cls._eval_expr(expr))
                except Exception:
                    return "N/A"
            # 路径引用 {{data.xxx}} / {{data}}
            if inner.startswith("data"):
                v = _path_value(inner)
                return "N/A" if v is None else v
            return m.group(0)

        # 1) 双层大括号块
        result = re.sub(r"\{\{(.*?)\}\}", _render_block, template)

        # 2) 单层大括号兼容 {data.x}
        def _single_repl(m):
            v = _path_value(m.group(1)[1:-1].strip())
            return "N/A" if v is None else v

        result = re.sub(r"(\{data\.[^{}]+\})", _single_repl, result)
        return result

    @staticmethod
    def _get_by_path(data, path: str):
        current = data
        for part in str(path).split("."):
            if not part:
                continue
            # 支持 list 下标 a.b[1].c
            if "[" in part:
                head, _, tail = part.partition("[")
                if head:
                    current = current.get(head) if isinstance(current, dict) else None
                try:
                    idx = int(tail.rstrip("]"))
                    current = current[idx] if isinstance(current, list) else None
                except Exception:
                    return None
            elif isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except Exception:
                    return None
            else:
                return None
        return current

    @staticmethod
    def _eval_expr(expr: str) -> Any:
        """安全计算数学表达式。"""
        result = expr.strip()
        result = result.replace("%", "/100")
        safe_funcs = {
            "abs": abs, "round": round, "min": min, "max": max,
            "pow": pow, "sqrt": math.sqrt, "floor": math.floor,
            "ceil": math.ceil, "log": math.log, "log10": math.log10,
            "exp": math.exp, "sin": math.sin, "cos": math.cos,
            "tan": math.tan, "pi": math.pi, "e": math.e,
        }
        try:
            val = eval(result, {"__builtins__": {}}, safe_funcs)
        except Exception:
            raise
        if isinstance(val, float):
            if val.is_integer():
                return int(val)
            return round(val, 2)
        return val

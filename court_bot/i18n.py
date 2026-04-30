from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.app_commands import TranslationContextTypes


class StaticExtrasTranslator(app_commands.Translator):
    """一个“静态字典翻译器”。

    用法：把需要本地化的字段写成 locale_str：
        locale_str('apply', zh_CN='申请议诉', zh_TW='申請議訴', en_US='申请议诉')

    然后在 Bot 启动时：
        bot.tree.set_translator(StaticExtrasTranslator())

    这样在 sync 指令时会把 name_localizations / description_localizations / option_localizations
    等字段带上，从而在 Discord 客户端显示中文。

    说明：
    - discord.py 的 locale_str 本身只是“待翻译的占位符”，没有 translator 时会退回 message（通常是英文/内部名）。
    - 我们在这里把 locale_str.extras 当作静态翻译表使用。
    """

    @staticmethod
    def _find_in_extras(extras: dict, locale: discord.Locale) -> Optional[str]:
        if not extras:
            return None

        # 例如 locale.value = 'zh-CN'
        candidates = [
            locale.value,  # zh-CN
            locale.value.replace('-', '_'),  # zh_CN
            locale.name,  # chinese / american_english ...
        ]

        for key in candidates:
            v = extras.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

        # 兜底：优先繁体，再简体，再英文（但内容我们也写中文）
        if locale == discord.Locale.taiwan_chinese:
            for key in ("zh_TW", "zh-TW", "zh_CN", "zh-CN", "en_US", "en-US", "en_GB", "en-GB"):
                v = extras.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        else:
            for key in ("zh_CN", "zh-CN", "zh_TW", "zh-TW", "en_US", "en-US", "en_GB", "en-GB"):
                v = extras.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()

        return None

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: TranslationContextTypes,
    ) -> Optional[str]:
        # 静态翻译：从 extras 里取
        translated = self._find_in_extras(string.extras, locale)
        if translated is not None:
            return translated

        # 没提供翻译就返回 None（Discord 会使用默认 message）
        return None

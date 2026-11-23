# -*- coding: utf-8 -*-
"""
Discord-бот: переводчик новостей.
Берёт сообщения из канала-источника (source), переводит их на русский
и отправляет в целевой канал (target).

Перевод работает напрямую через бесплатный эндпоинт Google Translate
(без сторонних библиотек-обёрток — только aiohttp, который уже есть в discord.py).

Токен берётся из переменной окружения DISCORD_TOKEN (задаётся на хостинге).
"""

import os
import json
import re
import aiohttp
import discord
from discord import app_commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CONFIG_PATH = "bindings.json"

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

http_session: aiohttp.ClientSession = None
translator_ok = True  # жив ли переводчик по последней проверке


# ---------------- Конфигурация привязок ----------------

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()


# ---------------- Перевод ----------------

def _split_chunks(text: str, limit: int = 1900) -> list:
    """Режет длинный текст на куски меньше лимита Discord."""
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # ищем ближайший перенос строки или пробел, чтобы не резать слово
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    return chunks


async def translate_text(text: str) -> tuple:
    """
    Переводит текст на русский.
    Возвращает (результат, успех).
    Если текст уже на русском или без букв — возвращает как есть.
    Если сервис недоступен — возвращает оригинал и False.
    """
    global translator_ok
    text = (text or "").strip()
    if not text:
        return text, True
    # нет латиницы/иероглифов — переводить нечего (уже русский)
    if not re.search(r"[A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF\u4E00-\u9FFF\u3040-\u30FF]", text):
        return text, True
    try:
        params = {"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text}
        async with http_session.get(GOOGLE_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        translated = "".join(part[0] for part in (data[0] or []) if part and part[0])
        if translated:
            translator_ok = True
            return translated, True
        return text, False
    except Exception:
        translator_ok = False
        return text, False


def _truncate(s: str, limit: int) -> str:
    if s is None:
        return None
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ---------------- Пересылка сообщения ----------------

async def rebuild_embed(message: discord.Message) -> list:
    """Переводит текстовые поля embed-карточек и возвращает список новых embed-ов."""
    result = []
    for emb in message.embeds:
        e = discord.Embed()
        if emb.title:
            t, _ = await translate_text(emb.title)
            e.title = _truncate(t, 256)
        if emb.description:
            d, _ = await translate_text(emb.description)
            e.description = _truncate(d, 4000)
        if emb.url:
            e.url = emb.url
        for field in emb.fields:
            fname, _ = await translate_text(field.name)
            fvalue, _ = await translate_text(field.value)
            e.add_field(name=_truncate(fname, 256), value=_truncate(fvalue, 1000), inline=field.inline)
        if emb.footer and emb.footer.text:
            ft, _ = await translate_text(emb.footer.text)
            e.set_footer(text=_truncate(ft, 200))
            if emb.footer.icon_url:
                e.set_footer(text=_truncate(ft, 200), icon_url=emb.footer.icon_url)
        if emb.author:
            if emb.author.name:
                an, _ = await translate_text(emb.author.name)
                e.set_author(name=_truncate(an, 256), url=emb.author.url, icon_url=emb.author.icon_url)
        if emb.image and emb.image.url:
            e.set_image(url=emb.image.url)
        if emb.thumbnail and emb.thumbnail.url:
            e.set_thumbnail(url=emb.thumbnail.url)
        if emb.colour:
            e.colour = emb.colour
        result.append(e)
    return result


async def forward_message(message: discord.Message, cfg: dict) -> None:
    guild = message.guild
    if not guild:
        return
    target = guild.get_channel(cfg.get("target_id"))
    if target is None:
        return

    # 1. Переводим текст сообщения
    translated, ok = await translate_text(message.content or "")
    prefix = "" if ok else "🌐 Переводчик недоступен — отправлен оригинал:\n"
    who = f"**{message.author.display_name}:**\n" if message.author.display_name else ""

    for chunk in _split_chunks(prefix + who + translated):
        try:
            await target.send(chunk)
        except discord.HTTPException:
            pass

    # 2. Переводим embed-карточки
    if message.embeds:
        embeds = await rebuild_embed(message)
        for i in range(0, len(embeds), 10):
            try:
                await target.send(embeds=embeds[i:i + 10])
            except discord.HTTPException:
                pass

    # 3. Копируем картинки-вложения
    image_urls = [a.url for a in message.attachments if a.content_type and a.content_type.startswith("image/")]
    if image_urls:
        for i in range(0, len(image_urls), 10):
            batch = [discord.Embed().set_image(url=u) for u in image_urls[i:i + 10]]
            try:
                await target.send(embeds=batch)
            except discord.HTTPException:
                pass


# ---------------- События ----------------

@client.event
async def on_ready():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    await tree.sync()
    try:
        await client.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="за новостями"))
    except Exception:
        pass
    print(f"✅ Бот запущен как {client.user} (guilds: {len(client.guilds)})")


@client.event
async def on_message(message: discord.Message):
    # игнорируем собственные сообщения бота
    if message.author.id == client.user.id:
        return
    cfg = config.get(str(message.guild.id)) if message.guild else None
    if not cfg or message.channel.id != cfg.get("source_id"):
        return
    if message.type not in (discord.MessageType.default, discord.MessageType.news, discord.MessageType.reply):
        return
    await forward_message(message, cfg)


# ---------------- Слэш-команды ----------------

def is_admin(interaction: discord.Interaction) -> bool:
    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.id == interaction.guild.owner_id
    )


@tree.command(name="bind", description="Привязать каналы: source — откуда брать новости, target — куда переводить")
@app_commands.describe(source="Канал с оригинальными новостями", target="Канал для переведённых новостей")
async def bind(interaction: discord.Interaction, source: discord.TextChannel, target: discord.TextChannel):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Только администратор может настраивать привязку.", ephemeral=True)
        return
    config[str(interaction.guild_id)] = {"source_id": source.id, "target_id": target.id}
    save_config(config)
    await interaction.response.send_message(
        f"✅ Привязано: `{source.name}` → `{target.name}`. Теперь сообщения из {source.mention} "
        f"будут переводиться в {target.mention}.",
        ephemeral=True,
    )


@tree.command(name="translate", description="Перевести текст на русский (тест переводчика)")
@app_commands.describe(text="Текст для перевода")
async def translate(interaction: discord.Interaction, text: str):
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    await interaction.response.defer(ephemeral=True, thinking=True)
    translated, ok = await translate_text(text)
    if ok:
        await interaction.followup.send(f"🌐 **Перевод:**\n{translated}", ephemeral=True)
    else:
        await interaction.followup.send(
            "⚠️ Переводчик недоступен (сервис не ответил). Попробуйте ещё раз через минуту.",
            ephemeral=True,
        )


@tree.command(name="check", description="Проверить: права в каналах и работоспособность переводчика")
async def check(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    lines = []

    cfg = config.get(str(interaction.guild_id))
    if not cfg:
        lines.append("❌ Привязка не настроена — выполните `/bind`.")
    else:
        src = interaction.guild.get_channel(cfg.get("source_id"))
        tgt = interaction.guild.get_channel(cfg.get("target_id"))
        me = interaction.guild.me

        if src is None:
            lines.append("❌ Канал-источник (source) не найден.")
        else:
            perms = src.permissions_for(me)
            lines.append(f"{'✅' if perms.view_channel and perms.read_message_history else '❌'} "
                         f"Источник {src.mention}: вижу {'да' if perms.view_channel and perms.read_message_history else 'НЕТ'}")

        if tgt is None:
            lines.append("❌ Целевой канал (target) не найден.")
        else:
            perms = tgt.permissions_for(me)
            ok = perms.send_messages and perms.embed_links and perms.attach_files
            lines.append(f"{'✅' if ok else '❌'} Целевой {tgt.mention}: писать/встраивать "
                         f"{'могу' if ok else 'НЕ могу'}")

    # тест переводчика
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    test, ok = await translate_text("Hello, this is a test message.")
    if ok and test.lower() != "hello, this is a test message.":
        lines.append(f"✅ Переводчик работает: «{test}»")
    else:
        lines.append("❌ Переводчик не отвечает — переводы пойдут в оригинале.")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ---------------- Запуск ----------------

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ Переменная окружения DISCORD_TOKEN не задана!")
    client.run(DISCORD_TOKEN)

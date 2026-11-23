# -*- coding: utf-8 -*-
"""
Discord-бот: автоматический перевод новостей EN -> RU.

Что делает:
  • Слушает промежуточный канал (куда Discord пересылает новости подпиской
    на чужой канал объявлений). Вебхуки НЕ игнорируются.
  • Переводит текст (content + embed-карточки) на русский через deep-translator.
  • Отправляет в основной канал: перевод + та же картинка (из embed по URL
    или вложением, скачанным ботом).
  • Выдаёт владельцу (OWNER_ID) роль «Высшая админка» на своём сервере.
  • /bind — привязать каналы, /check — проверить коннект, /translate — тест.
"""

import json
import logging
import os
import time
import deep_translator
print("✅ deep_translator найден:", deep_translator.__version__)
import discord
from discord import app_commands
from deep_translator import GoogleTranslator

# ════════════════════════ НАСТРОЙКИ ════════════════════════

TOKEN = os.getenv("DISCORD_TOKEN", "")

OWNER_ID = 1411647769084563458          # твой ID — высшая админка
ADMIN_ROLE_NAME = "Высшая админка"      # роль, которую бот выдаст тебе

# Промежуточный канал НА ТВОЁМ сервере (куда приходит подписка с новостями).
# УЗНАЙ ЕГО ID: правый клик по каналу -> «Копировать ID канала», и впиши сюда.
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0"))

# Основной канал, куда бот отправляет перевод.
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "1566145147111219281"))

CONFIG_FILE = "config.json"
TRANSLATE_RETRIES = 3
RETRY_DELAY_SEC = 3
DISCORD_LIMIT = 2000
MAX_EMBEDS = 10

# ════════════════════════ ЛОГИРОВАНИЕ ════════════════════════

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("news-translator")

translator = GoogleTranslator(source="auto", target="ru")

# ════════════════════════ КОНФИГ (переживает рестарт) ════════════════════════

config: dict = {"source_id": SOURCE_CHANNEL_ID, "target_id": TARGET_CHANNEL_ID}

def load_config():
    global config
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        config["source_id"] = int(data.get("source_id", SOURCE_CHANNEL_ID))
        config["target_id"] = int(data.get("target_id", TARGET_CHANNEL_ID))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# ════════════════════════ КЛИЕНТ ════════════════════════

intents = discord.Intents.default()
intents.message_content = True   # ОБЯЗАТЕЛЬНО: иначе текст сообщений пустой
intents.guilds = True
intents.members = True           # чтобы находить тебя и выдать роль

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ════════════════════════ ТЕКСТ И ПЕРЕВОД ════════════════════════

def split_text(text: str, limit: int) -> list:
    """Режет текст на куски не длиннее limit, не рвя слова."""
    if not text:
        return []
    pieces = []
    for block in text.split("\n"):
        while len(block) > limit:
            cut = block.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            pieces.append(block[:cut].rstrip())
            block = block[cut:].lstrip()
        if block:
            pieces.append(block)
    messages, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 <= limit:
            current += "\n" + piece
        elif current:
            messages.append(current)
            current = piece
        else:
            current = piece
    if current:
        messages.append(current)
    return messages

def is_mostly_russian(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    cyrillic = sum(1 for c in letters if "а" <= c.lower() <= "я")
    return cyrillic / len(letters) > 0.3

def translate_text(text: str):
    """Перевод с повторами при сбоях. None — если не удалось вовсе."""
    if not text or not text.strip():
        return text
    parts = []
    for chunk in split_text(text, 4500):
        for attempt in range(1, TRANSLATE_RETRIES + 1):
            try:
                result = translator.translate(chunk)
                if result is None or not result.strip():
                    raise ValueError("Пустой ответ переводчика")
                parts.append(result)
                break
            except Exception as e:
                log.warning("Попытка перевода %d/%d: %s", attempt, TRANSLATE_RETRIES, e)
                if attempt < TRANSLATE_RETRIES:
                    time.sleep(RETRY_DELAY_SEC * attempt)
                else:
                    return None
    return "\n".join(parts)

def safe_translate(text: str) -> str:
    """Никогда не падает: при ошибке вернёт оригинал."""
    try:
        translated = translate_text(text)
        if translated and translated.strip():
            return translated
    except Exception:
        log.exception("Непредвиденная ошибка перевода")
    return text

def extract_text(message: discord.Message) -> str:
    """Весь текст: content + все embed-карточки."""
    parts = []
    if message.content.strip():
        parts.append(message.content.strip())
    for embed in message.embeds:
        lines = []
        if embed.author and embed.author.name:
            lines.append(embed.author.name)
        if embed.title:
            lines.append(embed.title)
        if embed.description:
            lines.append(embed.description)
        for field in embed.fields:
            if field.name:
                lines.append(field.name)
            if field.value:
                lines.append(field.value)
        if embed.footer and embed.footer.text:
            lines.append(embed.footer.text)
        if lines:
            parts.append("\n".join(lines))
    return "\n\n".join(parts)

def extract_text_of_embed(embed: discord.Embed) -> str:
    lines = []
    if embed.title:
        lines.append(embed.title)
    if embed.description:
        lines.append(embed.description)
    for field in embed.fields:
        if field.name:
            lines.append(field.name)
        if field.value:
            lines.append(field.value)
    return "\n".join(lines)

# ════════════════════════ ОТПРАВКА ════════════════════════

async def send_chunks(target, text: str, files):
    chunks = split_text(text, DISCORD_LIMIT)
    for i, chunk in enumerate(chunks):
        await target.send(chunk,
                          files=files if i == 0 else None,
                          allowed_mentions=discord.AllowedMentions.none())
        files = None

async def send_translated_embeds(message: discord.Message, target, files):
    """Пересобирает карточки с переводом. Картинки — те же (по URL)."""
    extra_text = []
    first = True
    for original in message.embeds[:MAX_EMBEDS]:
        try:
            t_title = safe_translate(original.title) if original.title else ""
            t_desc = safe_translate(original.description) if original.description else ""
            embed = discord.Embed(title=t_title[:256] or None,
                                  description=t_desc[:4096] or None)
            if len(t_desc) > 4096:
                extra_text.append(t_desc[4096:])
            if original.url:
                embed.url = original.url
            if original.color is not None:
                embed.color = original.color
            if original.image:
                embed.set_image(url=original.image.url)      # та же картинка
            if original.thumbnail:
                embed.set_thumbnail(url=original.thumbnail.url)
            if original.author and original.author.name:
                embed.set_author(name=safe_translate(original.author.name)[:256],
                                 url=original.author.url,
                                 icon_url=original.author.icon_url)
            if original.footer and original.footer.text:
                embed.set_footer(text=safe_translate(original.footer.text)[:2048])
            for field in original.fields[:25]:
                name = safe_translate(field.name)[:256] if field.name else "\u200b"
                value = safe_translate(field.value)[:1024] if field.value else "\u200b"
                embed.add_field(name=name, value=value, inline=field.inline)
            await target.send(embed=embed, files=files if first else None,
                              allowed_mentions=discord.AllowedMentions.none())
            first = False
        except Exception:
            log.exception("Не удалось пересобрать embed, отправляю текстом")
            await send_chunks(target, safe_translate(extract_text_of_embed(original)),
                              files if first else [])
            first = False
    content = message.content.strip()
    if content:
        duplicated = any(content in (e.description or "") or content in (e.title or "")
                         for e in message.embeds)
        if not duplicated:
            await send_chunks(target, safe_translate(content), files if first else [])
            first = False
    for part in extra_text:
        await send_chunks(target, safe_translate(part), [])

async def process_news(message: discord.Message):
    """Главная обработка одной новости."""
    target = bot.get_channel(config["target_id"])
    if target is None:
        try:
            target = await bot.fetch_channel(config["target_id"])
        except (discord.NotFound, discord.Forbidden):
            target = None
    if target is None:
        log.warning("Основной канал (ID %s) не найден. Проверь /bind и права.",
                    config["target_id"])
        return

    raw_text = extract_text(message)
    if not raw_text and not message.attachments and not message.embeds:
        return  # пустое сообщение — пропускаем

    # Скачиваем вложения (картинки/файлы), чтобы отправить их же в перевод
    files = []
    for attachment in message.attachments:
        already_in_embed = any(e.image and e.image.url == attachment.url
                               for e in message.embeds)
        if already_in_embed:
            continue  # картинка уже будет в карточке — не дублируем
        try:
            files.append(await attachment.to_file())
        except Exception:
            log.warning("Вложение %s не скачалось, пришлю ссылку", attachment.filename)
            raw_text += f"\n\nВложение: {attachment.url}"

    # Уже русский текст — просто пересылаем без перевода
    if is_mostly_russian(raw_text):
        if raw_text:
            await send_chunks(target, raw_text, files)
        elif files:
            await target.send(files=files)
        return

    if message.embeds:
        await send_translated_embeds(message, target, files)
    else:
        await send_chunks(target, safe_translate(raw_text), files)

    log.info("Новость (id=%s) переведена и отправлена", message.id)

# ════════════════════════ ПРАВА ════════════════════════

def is_owner(user) -> bool:
    return user.id == OWNER_ID

# ════════════════════════ КОМАНДЫ ════════════════════════

@tree.command(name="bind", description="Привязать каналы (только владелец)")
@app_commands.describe(source="Канал, откуда бот читает новости",
                       target="Канал, куда бот отправляет перевод")
async def bind(interaction: discord.Interaction, source: discord.TextChannel,
               target: discord.TextChannel):
    if not is_owner(interaction.user):
        await interaction.response.send_message("⛔ Только владелец бота.", ephemeral=True)
        return
    config["source_id"] = source.id
    config["target_id"] = target.id
    save_config()
    await interaction.response.send_message(
        f"✅ Привязано!\n• Слежу за: {source.mention}\n• Отправляю в: {target.mention}",
        ephemeral=True)

@tree.command(name="check", description="Проверить коннект: Discord, переводчик, каналы")
async def check(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    lines = []
    latency = round(bot.latency * 1000)
    lines.append(f"🔌 Коннект Discord: **{latency} мс** — "
                 + ("всё хорошо" if latency < 500 else "медленно, но работает"))

    src = bot.get_channel(config["source_id"])
    tgt = bot.get_channel(config["target_id"])
    lines.append(f"📥 Промежуточный канал: {src.mention if src else '❌ не привязан — /bind'}")
    if isinstance(src, discord.TextChannel):
        perms = src.permissions_for(src.guild.me)
        lines.append(f"   └ Чтение: {'✅' if perms.view_channel else '❌ нет права View Channel'}")
    lines.append(f"📤 Основной канал: {tgt.mention if tgt else '❌ не привязан — /bind'}")
    if isinstance(tgt, discord.TextChannel):
        perms = tgt.permissions_for(tgt.guild.me)
        lines.append(f"   └ Отправка: {'✅' if perms.send_messages else '❌'}")
        lines.append(f"   └ Вложения/карточки: "
                     f"{'✅' if perms.attach_files and perms.embed_links else '❌'}")

    test_start = time.time()
    try:
        translated = translator.translate("Hello, this is a connection test.")
    except Exception:
        translated = None
    if translated:
        lines.append(f"🌐 Переводчик: ✅ ({round((time.time() - test_start) * 1000)} мс), "
                     f"тест: «{translated}»")
    else:
        lines.append("🌐 Переводчик: ❌ не отвечает — переводы пойдут в оригинале")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

@tree.command(name="translate", description="Перевести текст EN→RU (тест переводчика)")
@app_commands.describe(text="Текст для перевода")
async def translate_cmd(interaction: discord.Interaction, text: str):
    if is_mostly_russian(text):
        await interaction.response.send_message("ℹ️ Текст уже на русском.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(safe_translate(text)[:2000], ephemeral=True)

# ════════════════════════ СОБЫТИЯ ════════════════════════

@bot.event
async def on_ready():
    log.info("Бот запущен: %s (id=%s)", bot.user, bot.user.id)
    load_config()

    try:
        await tree.sync()
        log.info("Слеш-команды синхронизированы (/bind, /check, /translate)")
    except Exception:
        log.exception("Не удалось синхронизировать команды")

    # Выдаём высшую админку владельцу
    for guild in bot.guilds:
        try:
            member = guild.get_member(OWNER_ID) or await guild.fetch_member(OWNER_ID)
            role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
            if role is None:
                role = await guild.create_role(
                    name=ADMIN_ROLE_NAME,
                    permissions=discord.Permissions.all(),
                    color=discord.Color.red(),
                    reason="Высшая админка владельца бота")
            if role not in member.roles:
                await member.add_roles(role, reason="Выдача высшей админки владельцу")
                log.info("Роль «%s» выдана на сервере %s", ADMIN_ROLE_NAME, guild.name)
        except discord.Forbidden:
            log.warning("Не могу выдать роль на %s — подними роль бота выше «%s» "
                        "в списке ролей сервера.", guild.name, ADMIN_ROLE_NAME)
        except discord.NotFound:
            log.info("Владелец (ID %s) не найден на сервере %s", OWNER_ID, guild.name)
        except Exception:
            log.exception("Ошибка выдачи роли на %s", guild.name)

    log.info("Источник: %s | Приёмник: %s",
             f"#{bot.get_channel(config['source_id'])}" if config["source_id"]
             else "❌ не привязан — /bind",
             f"#{bot.get_channel(config['target_id'])}" if config["target_id"]
             else "❌ не привязан — /bind")

@bot.event
async def on_message(message: discord.Message):
    # Вебхуки НЕ игнорируем — новости приходят именно от них.
    if message.author.id == bot.user.id:
        return
    if message.channel.id != config["source_id"]:
        return
    try:
        await process_news(message)
    except Exception:
        log.exception("Ошибка при обработке сообщения (id=%s)", message.id)

# ════════════════════════ ЗАПУСК ════════════════════════

if __name__ == "__main__":
    load_config()
    if not TOKEN:
        log.error("Токен не задан! Укажи переменную окружения DISCORD_TOKEN на хостинге.")
    else:
        bot.run(TOKEN)

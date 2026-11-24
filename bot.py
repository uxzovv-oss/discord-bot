import os
import io
import discord
from discord import app_commands
import aiohttp

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise SystemExit("❌ Не найдена переменная окружения DISCORD_TOKEN")

# ============================================================
#  ДОСТУП: команды работают ТОЛЬКО у тех, чей ID вписан ниже.
#  Достать ID: Настройки Discord -> Расширенные -> Режим разработчика,
#  потом ПКМ по юзеру -> Копировать ID пользователя.
# ============================================================
ALLOWED_USER_IDS = [
    123456789012345678,  # <- ЗАМЕНИ на свой ID
    # 987654321098765432,  # <- второй админ, если нужен
]

# Роль, которая тоже даёт доступ (не требует админки!)
ROLE_NAME = "not-bot"

# Имя, от которого вебхук шлёт сообщения по умолчанию
DEFAULT_WEBHOOK_NAME = "Bodycam News Bot"

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Привязки каналов: guild_id -> {"source": id, "target": id}
bindings = {}

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"

MAX_FILE_SIZE = 8 * 1024 * 1024
TRANSLATE_CHUNK = 450


def has_access(interaction: discord.Interaction) -> bool:
    """Жёсткая проверка доступа: только ID из списка или роль not-bot."""
    if interaction.user.id in ALLOWED_USER_IDS:
        return True
    roles = getattr(interaction.user, "roles", [])
    return any(role.name.lower() == ROLE_NAME.lower() for role in roles)


async def access_denied(interaction: discord.Interaction):
    await interaction.response.send_message(
        "⛔ Нет доступа к командам бота.",
        ephemeral=True,
    )


# ============================================================
#  ПЕРЕВОДЧИК
# ============================================================

def split_for_translation(text, limit=TRANSLATE_CHUNK):
    """Режет текст на куски не длиннее limit, не рвя слова посередине."""
    parts = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def is_bad_translation(result: str) -> bool:
    """Отсеивает ответы-заглушки вроде 'QUERY LENGTH LIMIT EXCEEDED'."""
    if not result:
        return True
    bad_markers = ("QUERY LENGTH", "LIMIT EXCEEDED", "MYMEMORY WARNING")
    upper = result.upper()
    return any(marker in upper for marker in bad_markers)


async def google_translate(text: str, dest: str = "ru"):
    """Перевод через Google с двумя попытками. None — если не вышло."""
    if not text or not text.strip():
        return None
    params = {"client": "gtx", "sl": "auto", "tl": dest, "dt": "t", "q": text}
    timeout = aiohttp.ClientTimeout(total=15)
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(GOOGLE_URL, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = "".join(p[0] for p in data[0] if p and p[0])
                        if result.strip() and not is_bad_translation(result):
                            return result
        except Exception as err:
            print(f"⚠️ Google (попытка {attempt + 1}): {err}")
    return None


async def mymemory_translate(text: str, dest: str = "ru"):
    """Запасной переводчик MyMemory."""
    if not text or not text.strip():
        return None
    results = []
    for chunk in split_for_translation(text, 450):
        params = {"q": chunk, "langpair": f"en|{dest}"}
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(MYMEMORY_URL, params=params) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            piece = data.get("responseData", {}).get("translatedText")
            if not piece or is_bad_translation(piece):
                return None
            results.append(piece)
        except Exception as err:
            print(f"⚠️ MyMemory: {err}")
            return None
    return " ".join(results)


async def translate_text(text: str, dest: str = "ru"):
    """Google -> MyMemory, длинные тексты — кусками."""
    if not text or not text.strip():
        return None
    pieces = split_for_translation(text)
    translated_parts = []
    for chunk in pieces:
        result = await google_translate(chunk, dest)
        if result is None:
            result = await mymemory_translate(chunk, dest)
        if result is None:
            return None
        translated_parts.append(result)
    return " ".join(translated_parts).strip()


async def download_file(url: str):
    """Скачивает вложение и возвращает discord.File. None — если не удалось."""
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        if len(data) > MAX_FILE_SIZE:
            return None
        filename = url.split("/")[-1].split("?")[0] or "file.png"
        return discord.File(io.BytesIO(data), filename=filename)
    except Exception as err:
        print(f"⚠️ Не удалось скачать файл {url}: {err}")
        return None


def chunk_text(text, limit=1900):
    """Режет длинный текст под лимит Discord 2000 символов."""
    parts = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut])
        text = text[cut + 1:]
    if text:
        parts.append(text)
    return parts


def extract_message_payload(message: discord.Message):
    """Достаёт текст, эмбеды и вложения — в том числе из пересланных сообщений."""
    text = message.content or ""
    embeds = list(message.embeds)
    attachments = list(message.attachments)

    if message.message_snapshots:
        snap = message.message_snapshots[0].message
        if not text.strip():
            text = snap.content or ""
        if not embeds:
            embeds = list(snap.embeds)
        if not attachments:
            attachments = list(snap.attachments)

    return text, embeds, attachments


# ============================================================
#  ВЕБХУКИ
# ============================================================

async def get_webhook(channel: discord.TextChannel):
    """Находит вебхук бота в канале или создаёт новый. None — если прав нет."""
    try:
        for wh in await channel.webhooks():
            if wh.user == bot.user:
                return wh
        return await channel.create_webhook(name=DEFAULT_WEBHOOK_NAME)
    except discord.Forbidden:
        print(f"❌ Нет прав создавать вебхук в #{channel.name}")
        return None
    except Exception as err:
        print(f"⚠️ Ошибка вебхука: {err}")
        return None


async def send_as_webhook(channel, content=None, files=None, embeds=None,
                          username=DEFAULT_WEBHOOK_NAME, avatar_url=None):
    """Шлёт сообщение через вебхук от имени бота. Если вебхук недоступен — обычным способом."""
    wh = await get_webhook(channel)
    try:
        if wh is not None:
            await wh.send(
                content=content,
                files=files,
                embeds=embeds,
                username=username,
                avatar_url=avatar_url,
            )
        else:
            await channel.send(content=content, files=files, embeds=embeds)
    except discord.Forbidden:
        raise


# ============================================================
#  КОМАНДА /SAY — отправка сообщения вебхуком в выбранный канал
# ============================================================

class SayChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="Куда отправить сообщение",
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await interaction.response.send_modal(SayModal(channel))


class SayModal(discord.ui.Modal, title="Сообщение от имени бота"):
    author_name = discord.ui.TextInput(
        label="Имя отправителя (как будет видно в канале)",
        placeholder=f"Например: {DEFAULT_WEBHOOK_NAME}",
        default=DEFAULT_WEBHOOK_NAME,
        required=False,
        max_length=60,
    )
    avatar_url = discord.ui.TextInput(
        label="Ссылка на аватарку (необязательно)",
        placeholder="https://... (прямая ссылка на картинку)",
        required=False,
        max_length=300,
    )
    embed_title = discord.ui.TextInput(
        label="Заголовок (тема)",
        placeholder="Например: 📢 Новый патч Bodycam!",
        required=False,
        max_length=256,
    )
    message_text = discord.ui.TextInput(
        label="Текст сообщения",
        style=discord.TextStyle.long,
        placeholder="Что написать в канал...",
        required=True,
        max_length=1800,
    )
    embed_color = discord.ui.TextInput(
        label="Цвет полосы (HEX)",
        placeholder="Например: #FF5500 или red / green / blue",
        default="#FF5500",
        required=False,
        max_length=20,
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # разбираем цвет
        color = discord.Color.orange()
        raw = self.embed_color.value.strip()
        color_map = {
            "red": discord.Color.red(),
            "green": discord.Color.green(),
            "blue": discord.Color.blue(),
            "yellow": discord.Color.gold(),
            "purple": discord.Color.purple(),
            "orange": discord.Color.orange(),
            "pink": discord.Color.magenta(),
            "black": discord.Color.dark_grey(),
        }
        if raw.lower() in color_map:
            color = color_map[raw.lower()]
        elif raw.startswith("#") and len(raw) == 7:
            try:
                color = discord.Color(int(raw[1:], 16))
            except ValueError:
                pass

        embed = discord.Embed(
            title=self.embed_title.value or None,
            description=self.message_text.value,
            color=color,
        )
        embed.set_footer(text=f"Отправлено: {interaction.user.display_name}")

        username = self.author_name.value.strip() or DEFAULT_WEBHOOK_NAME
        avatar = self.avatar_url.value.strip() or None

        try:
            await send_as_webhook(
                self.channel,
                embeds=[embed],
                username=username,
                avatar_url=avatar,
            )
            await interaction.followup.send(
                f"✅ Сообщение отправлено в <#{self.channel.id}> от имени **{username}**"
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Нет прав писать в <#{self.channel.id}>"
            )
        except Exception as err:
            await interaction.followup.send(f"❌ Ошибка отправки: {err}")


class SayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(SayChannelSelect())


@tree.command(
    name="say",
    description="Отправить сообщение вебхуком в выбранный канал (только для админов)",
)
async def say(interaction: discord.Interaction):
    if not has_access(interaction):
        await access_denied(interaction)
        return
    await interaction.response.send_message(
        "Выбери канал, куда отправить сообщение:",
        view=SayView(),
        ephemeral=True,
    )


# ============================================================
#  КОМАНДА /BIND — привязка source и target
# ============================================================

class TargetSelect(discord.ui.ChannelSelect):
    def __init__(self, view):
        super().__init__(
            placeholder="Канал-приёмник (куда отправлять перевод)",
            channel_types=[discord.ChannelType.text],
        )
        self.parent_view = view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.target = self.values[0]
        bindings[interaction.guild_id] = {
            "source": self.parent_view.source.id,
            "target": self.parent_view.target.id,
        }
        await interaction.response.edit_message(
            content=(
                f"✅ Готово!\n"
                f"**Source (оригиналы):** <#{self.parent_view.source.id}>\n"
                f"**Target (переводы):** <#{self.parent_view.target.id}>\n"
                f"Теперь напиши `/check`."
            ),
            view=None,
        )


class SourceSelect(discord.ui.ChannelSelect):
    def __init__(self, view):
        super().__init__(
            placeholder="Канал-источник (куда падают оригиналы)",
            channel_types=[discord.ChannelType.text],
        )
        self.parent_view = view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.source = self.values[0]
        self.parent_view.remove_item(self)
        self.parent_view.add_item(TargetSelect(self.parent_view))
        await interaction.response.edit_message(
            content="Теперь выбери канал-приёмник (куда отправлять перевод):",
            view=self.parent_view,
        )


class BindView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.source = None
        self.target = None
        self.add_item(SourceSelect(self))


@tree.command(name="bind", description="Привязать каналы: source (оригиналы) и target (переводы)")
async def bind(interaction: discord.Interaction):
    if not has_access(interaction):
        await access_denied(interaction)
        return
    await interaction.response.send_message(
        "Выбери канал-источник (куда падают оригиналы новостей):",
        view=BindView(),
        ephemeral=True,
    )


# ============================================================
#  КОМАНДА /CHECK
# ============================================================

@tree.command(name="check", description="Проверить бота, каналы, права и переводчик")
async def check(interaction: discord.Interaction):
    if not has_access(interaction):
        await access_denied(interaction)
        return
    await interaction.response.defer(ephemeral=True)
    lines = ["✅ Бот работает"]
    lines.append(f"✅ Доступ есть: твой ID в списке или у тебя роль `{ROLE_NAME}`")

    b = bindings.get(interaction.guild_id)
    if not b:
        lines.append("❌ Привязка не настроена — сначала используй /bind")
    else:
        me = interaction.guild.me
        source = interaction.guild.get_channel(b["source"])
        target = interaction.guild.get_channel(b["target"])

        if source is None:
            lines.append("❌ Source: канал не найден, сделай /bind заново")
        else:
            perms = source.permissions_for(me)
            ok = perms.read_messages and perms.read_message_history
            lines.append(f"{'✅' if ok else '❌'} Source <#{source.id}>: читать={perms.read_messages}")

        if target is None:
            lines.append("❌ Target: канал не найден, сделай /bind заново")
        else:
            perms = target.permissions_for(me)
            wh_ok = perms.manage_webhooks
            lines.append(
                f"{'✅' if wh_ok else '❌'} Target <#{target.id}>: вебхуки={wh_ok}, писать={perms.send_messages}"
            )

    long_test = "hello world. " * 40
    test = await translate_text(long_test)
    if test and test.strip().lower() != long_test.strip().lower():
        lines.append("✅ Переводчик работает (в том числе с длинными текстами)")
    else:
        lines.append("❌ Переводчик недоступен — новости будут уходить в оригинале")

    await interaction.followup.send("\n".join(lines))


# ============================================================
#  КОМАНДА /TRANSLATE
# ============================================================

@tree.command(name="translate", description="Перевести текст на русский")
@app_commands.describe(text="Текст для перевода")
async def translate(interaction: discord.Interaction, text: str):
    if not has_access(interaction):
        await access_denied(interaction)
        return
    await interaction.response.defer(ephemeral=True)
    result = await translate_text(text)
    if result is None:
        await interaction.followup.send("❌ Переводчик недоступен — оба сервиса не ответили")
    else:
        await interaction.followup.send(
            f"**Оригинал:**\n{text[:1500]}\n\n**Перевод:**\n{result[:1500]}"
        )


# ============================================================
#  ПРИЁМ НОВОСТЕЙ (source -> target)
# ============================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return
    if message.guild is None:
        return

    b = bindings.get(message.guild.id)
    if not b or message.channel.id != b["source"]:
        return

    # вебхуки и подписки обрабатываем, обычных ботов — игнорируем
    if message.author.bot and message.webhook_id is None:
        return

    target = message.guild.get_channel(b["target"])
    if target is None:
        print(f"⚠️ Target-канал {b['target']} не найден")
        return

    text, embeds, attachments = extract_message_payload(message)

    translated = None
    if text.strip():
        translated = await translate_text(text)
        if translated is None:
            print("⚠️ Переводчик недоступен — отправляю оригинал с пометкой")

    new_embeds = []
    for e in embeds[:10]:
        ne = discord.Embed()
        if e.title:
            ne.title = await translate_text(e.title) or e.title
        if e.description:
            ne.description = await translate_text(e.description) or e.description
        for f in e.fields[:10]:
            name = await translate_text(f.name) or f.name
            value = await translate_text(f.value) or f.value
            ne.add_field(name=name, value=value, inline=f.inline)
        if e.footer and e.footer.text:
            ne.set_footer(text=await translate_text(e.footer.text) or e.footer.text)
        if e.image and e.image.url:
            ne.set_image(url=e.image.url)
        if e.thumbnail and e.thumbnail.url:
            ne.set_thumbnail(url=e.thumbnail.url)
        if e.url:
            ne.url = e.url
        if e.color:
            ne.color = e.color
        new_embeds.append(ne)

    files = []
    failed_urls = []
    for att in attachments[:5]:
        f = await download_file(att.url)
        if f is not None:
            files.append(f)
        else:
            failed_urls.append(att.url)

    try:
        if translated:
            parts = chunk_text(translated)
            first = parts.pop(0)
            await send_as_webhook(target, content=first, files=files or None)
            for part in parts:
                await send_as_webhook(target, content=part)
        elif text.strip():
            parts = chunk_text("🌐 *(переводчик временно недоступен)*\n" + text)
            first = parts.pop(0)
            await send_as_webhook(target, content=first, files=files or None)
            for part in parts:
                await send_as_webhook(target, content=part)
        elif files:
            await send_as_webhook(target, files=files)
        if new_embeds:
            await send_as_webhook(target, embeds=new_embeds)
        if failed_urls:
            await send_as_webhook(target, content="📎 " + "\n".join(failed_urls))
    except discord.Forbidden:
        print(f"❌ Нет прав писать в канал #{target.name}")
    except Exception as err:
        print(f"⚠️ Ошибка отправки: {err}")

    await bot.process_commands(message)


@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Бот запущен как {bot.user}")


bot.run(DISCORD_TOKEN)

import os
import discord
from discord import app_commands
import aiohttp

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise SystemExit("❌ Не найдена переменная окружения DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Привязки каналов: guild_id -> {"source": id, "target": id}
bindings = {}

TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"


async def google_translate(text: str, dest: str = "ru"):
    """Перевод через бесплатный эндпоинт Google. Возвращает None, если сервис недоступен."""
    if not text or not text.strip():
        return None
    params = {"client": "gtx", "sl": "auto", "tl": dest, "dt": "t", "q": text}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(TRANSLATE_URL, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        result = "".join(part[0] for part in data[0] if part and part[0])
        return result if result.strip() else None
    except Exception as err:
        print(f"⚠️ Ошибка перевода: {err}")
        return None


def chunk_text(text, limit=1900):
    """Режет длинный текст на части под лимит Discord 2000 символов."""
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


# ---------- /bind ----------

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
    await interaction.response.send_message(
        "Выбери канал-источник (куда падают оригиналы новостей):",
        view=BindView(),
        ephemeral=True,
    )


# ---------- /check ----------

@tree.command(name="check", description="Проверить бота, каналы, права и переводчик")
async def check(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    lines = ["✅ Бот работает"]

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
            lines.append(
                f"{'✅' if ok else '❌'} Source <#{source.id}>: "
                f"читать={perms.read_messages}"
            )

        if target is None:
            lines.append("❌ Target: канал не найден, сделай /bind заново")
        else:
            perms = target.permissions_for(me)
            ok = perms.send_messages and perms.embed_links and perms.attach_files
            lines.append(
                f"{'✅' if ok else '❌'} Target <#{target.id}>: "
                f"писать={perms.send_messages}, embeds={perms.embed_links}, файлы={perms.attach_files}"
            )

    test = await google_translate("hello")
    if test and test.strip().lower() != "hello":
        lines.append(f"✅ Переводчик работает: hello → {test}")
    else:
        lines.append("❌ Переводчик недоступен — переводы пойдут в оригинале")

    await interaction.followup.send("\n".join(lines))


# ---------- /translate ----------

@tree.command(name="translate", description="Перевести текст на русский")
@app_commands.describe(text="Текст для перевода")
async def translate(interaction: discord.Interaction, text: str):
    await interaction.response.defer(ephemeral=True)
    result = await google_translate(text)
    if result is None:
        await interaction.followup.send("❌ Переводчик недоступен")
    else:
        await interaction.followup.send(
            f"**Оригинал:**\n{text[:1500]}\n\n**Перевод:**\n{result[:1500]}"
        )


# ---------- Приём новостей ----------

@bot.event
async def on_message(message: discord.Message):
    # не реагируем на свои же сообщения (защита от зацикливания)
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

    # переводим текст
    text = message.content or ""
    translated = None
    if text.strip():
        translated = await google_translate(text)
        if translated is None:
            print("⚠️ Переводчик недоступен — отправляю оригинал")
            translated = text

    # переводим embed-карточки
    new_embeds = []
    for e in message.embeds[:10]:
        ne = discord.Embed()
        if e.title:
            ne.title = await google_translate(e.title) or e.title
        if e.description:
            ne.description = await google_translate(e.description) or e.description
        for f in e.fields[:10]:
            name = await google_translate(f.name) or f.name
            value = await google_translate(f.value) or f.value
            ne.add_field(name=name, value=value, inline=f.inline)
        if e.footer and e.footer.text:
            ne.set_footer(text=await google_translate(e.footer.text) or e.footer.text)
        if e.url:
            ne.url = e.url
        if e.color:
            ne.color = e.color
        new_embeds.append(ne)

    # картинки и файлы — копируем ссылками
    attachments_text = "\n".join(a.url for a in message.attachments)

    try:
        if translated:
            for part in chunk_text(translated):
                await target.send(content=part)
        if new_embeds:
            await target.send(embeds=new_embeds)
        if attachments_text and not translated and not new_embeds:
            await target.send(content=attachments_text)
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

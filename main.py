import asyncio
import os

import aiohttp
import discord
import yaml
from discord.ext import commands
from dotenv import load_dotenv
from pymongo import AsyncMongoClient

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.config = CONFIG


@bot.event
async def on_ready():
    print(f"✅ Logado como {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(status=discord.Status.dnd, activity=discord.Game(name="osr membro simulator"))
    try:
        synced = await bot.tree.sync()
        print(f"🔄 {len(synced)} comando(s) sincronizado(s).")
    except Exception as e:
        print(f"Erro ao sincronizar comandos: {e}")


async def carregar_cogs():
    for arquivo in os.listdir("./cogs"):
        if arquivo.endswith(".py") and not arquivo.startswith("_"):
            extensao = f"cogs.{arquivo[:-3]}"
            try:
                await bot.load_extension(extensao)
                print(f"📦 Cog carregada: {extensao}")
            except Exception as e:
                print(f"❌ Erro ao carregar {extensao}: {e}")


async def main():
    async with bot:
        bot.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        await carregar_cogs()
        try:
            await bot.start(TOKEN)
        finally:
            await bot.http_session.close()
            await bot.mongo.close()


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

ELEICAO = 6257
URL_TSE = (
    f"https://resultados.tse.jus.br/oficial/ele2026/{ELEICAO}"
    f"/dados/br/br-c0001-e{ELEICAO:06d}-u.json"
)
CANAL_ID = 1556414195686178847
ARQUIVO_ESTADO = Path("data/apuracao.json")
INTERVALO_SEGUNDOS = 30
ESPERA_MAXIMA_SEGUNDOS = 300
ESPERA_BLOQUEIO_SEGUNDOS = 660


def formatar_numero(valor) -> str:
    return f"{int(valor):,}".replace(",", ".")


def ler_estado(caminho: Path) -> dict:
    if not caminho.exists():
        return {}
    try:
        estado = json.loads(caminho.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return estado if isinstance(estado, dict) else {}


def gravar_estado(caminho: Path, estado: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(caminho)


class Apuracao(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessao: aiohttp.ClientSession | None = None
        self.etag: str | None = None
        self.ultima_modificacao: str | None = None
        self.dados: dict | None = None
        self.assinatura: str | None = None
        self.mensagem: discord.Message | None = None
        self.mensagem_id: int | None = None
        self.falhas = 0

    async def cog_load(self):
        if CANAL_ID is None:
            log.error("CANAL_ID não configurado em apuracao.py, o painel não será publicado")
        estado = await asyncio.to_thread(ler_estado, ARQUIVO_ESTADO)
        if estado.get("canal_id") == CANAL_ID:
            self.mensagem_id = estado.get("mensagem_id")
        self.sessao = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.atualizar.start()

    async def cog_unload(self):
        self.atualizar.cancel()
        if self.sessao is not None:
            await self.sessao.close()

    async def obter_canal(self):
        return self.bot.get_channel(CANAL_ID) or await self.bot.fetch_channel(CANAL_ID)

    async def recuperar_mensagem(self):
        if CANAL_ID is None or self.mensagem_id is None:
            return
        try:
            canal = await self.obter_canal()
            self.mensagem = await canal.fetch_message(self.mensagem_id)
        except discord.NotFound:
            log.info("Mensagem salva não existe mais, será criada outra")
            self.mensagem_id = None
        except discord.HTTPException as erro:
            log.warning("Não consegui recuperar a mensagem salva: %r", erro)

    async def salvar_estado(self):
        estado = {
            "canal_id": CANAL_ID,
            "mensagem_id": self.mensagem.id,
            "eleicao": ELEICAO,
            "turno": self.dados["t"],
            "ultima_atualizacao_tse": f"{self.dados['dg']} {self.dados['hg']}",
            "gravado_em": discord.utils.utcnow().isoformat(),
        }
        await asyncio.to_thread(gravar_estado, ARQUIVO_ESTADO, estado)

    async def buscar(self) -> bool:
        headers = {"Accept": "application/json", "User-Agent": "apuracao-bot/1.0"}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.ultima_modificacao:
            headers["If-Modified-Since"] = self.ultima_modificacao
        async with self.sessao.get(URL_TSE, headers=headers) as resp:
            if resp.status == 304:
                return False
            resp.raise_for_status()
            texto = await resp.text(encoding="utf-8-sig")
            self.etag = resp.headers.get("ETag")
            self.ultima_modificacao = resp.headers.get("Last-Modified")
        self.dados = json.loads(texto)
        return True

    def montar_embed(self, dados: dict) -> discord.Embed:
        cargo = dados["carg"][0]
        candidatos = []
        for agrupamento in cargo["agr"]:
            for partido in agrupamento["par"]:
                for cand in partido["cand"]:
                    candidatos.append((cand, partido["sg"]))
        candidatos.sort(key=lambda item: (-int(item[0]["vap"]), int(item[0]["seq"])))

        linhas = []
        for posicao, (cand, sigla) in enumerate(candidatos, start=1):
            linha = f"**{posicao}. {cand['nmu'].title()}** ({sigla} {cand['n']})\n"
            linha += f"{formatar_numero(cand['vap'])} votos · {cand['pvap']}%"
            if cand.get("st"):
                linha += f" · {cand['st']}"
            elif cand.get("e") == "s":
                linha += " · Eleito ou 2º turno"
            linhas.append(linha)

        secoes = dados["s"]
        votos = dados["v"]

        embed = discord.Embed(
            title=f"Apuração · Presidente · {dados['t']}º turno",
            description="\n\n".join(linhas),
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Seções totalizadas",
            value=f"{formatar_numero(secoes['st'])} de {formatar_numero(secoes['ts'])} ({secoes['pst']}%)",
            inline=False,
        )
        embed.add_field(name="Votos válidos", value=formatar_numero(votos["vv"]))
        embed.add_field(name="Brancos", value=formatar_numero(votos["vb"]))
        embed.add_field(name="Nulos", value=formatar_numero(votos["vn"]))
        embed.set_footer(text=f"Fonte: TSE · gerado em {dados['dg']} {dados['hg']}")
        return embed

    async def publicar(self):
        if CANAL_ID is None or self.dados is None:
            return
        embed = self.montar_embed(self.dados)
        assinatura = hashlib.sha1(
            json.dumps(embed.to_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()
        if assinatura == self.assinatura and self.mensagem is not None:
            return

        if self.mensagem is not None:
            try:
                await self.mensagem.edit(embed=embed)
            except discord.NotFound:
                self.mensagem = None

        if self.mensagem is None:
            canal = await self.obter_canal()
            self.mensagem = await canal.send(embed=embed)

        self.assinatura = assinatura
        await self.salvar_estado()

    def registrar_falha(self, status: int | None):
        self.falhas += 1
        if status in (403, 429):
            espera = ESPERA_BLOQUEIO_SEGUNDOS
        else:
            espera = min(INTERVALO_SEGUNDOS * 2 ** self.falhas, ESPERA_MAXIMA_SEGUNDOS)
        self.atualizar.change_interval(seconds=espera)
        log.warning("TSE falhou (status=%s, falhas=%d), próxima tentativa em %ds", status, self.falhas, espera)

    @tasks.loop(seconds=INTERVALO_SEGUNDOS)
    async def atualizar(self):
        try:
            await self.buscar()
        except aiohttp.ClientResponseError as erro:
            self.registrar_falha(erro.status)
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as erro:
            log.warning("Erro ao buscar apuração: %r", erro)
            self.registrar_falha(None)
            return

        if self.falhas:
            self.falhas = 0
            self.atualizar.change_interval(seconds=INTERVALO_SEGUNDOS)

        try:
            await self.publicar()
        except (discord.HTTPException, KeyError, OSError) as erro:
            log.warning("Erro ao publicar apuração: %r", erro)

    @atualizar.before_loop
    async def antes_de_atualizar(self):
        await self.bot.wait_until_ready()
        await self.recuperar_mensagem()

    @app_commands.command(name="apuracao", description="Mostra a apuração atual da eleição presidencial")
    async def apuracao(self, interaction: discord.Interaction):
        if self.dados is None:
            await interaction.response.send_message(
                "Ainda não recebi dados do TSE, tente de novo em instantes.", ephemeral=True
            )
            return
        await interaction.response.send_message(embed=self.montar_embed(self.dados), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Apuracao(bot))

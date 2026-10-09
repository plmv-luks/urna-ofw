import discord
from discord import app_commands
from discord.ext import commands
from pymongo.errors import DuplicateKeyError

ESTADO = {"_id": "estado"}


def aviso(texto):
    view = discord.ui.LayoutView()
    view.add_item(discord.ui.TextDisplay(texto))
    return view


class Confirmacao(discord.ui.LayoutView):
    def __init__(self, cog, numero, candidato):
        super().__init__(timeout=60)
        self.cog = cog
        self.numero = numero

        confirmar = discord.ui.Button(label="Confirmar voto", style=discord.ButtonStyle.green)
        confirmar.callback = self.confirmar
        cancelar = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.gray)
        cancelar.callback = self.cancelar

        self.add_item(
            discord.ui.Container(
                discord.ui.Section(
                    "Seu voto vai pra:",
                    f"## {candidato.display_name}",
                    f"Número **{numero}**",
                    accessory=discord.ui.Thumbnail(candidato.display_avatar.url),
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(confirmar, cancelar),
                accent_color=discord.Color.blurple(),
            )
        )

    async def confirmar(self, interaction: discord.Interaction):
        self.stop()
        if self.numero not in await self.cog.candidatos():
            await interaction.response.edit_message(
                view=aviso("Eita, esse candidato saiu da disputa enquanto você votava. Tenta de novo.")
            )
            return

        res = await self.cog.registrar_voto(interaction.user.id, self.numero)
        msgs = {
            "ok": f"Pronto, seu voto no {self.numero} foi computado. Valeu por participar!",
            "repetido": "Você já votou, só vale uma vez.",
            "fechada": "Chegou tarde, a votação já fechou.",
        }
        await interaction.response.edit_message(view=aviso(msgs[res]))

    async def cancelar(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(
            view=aviso("Beleza, nada foi registrado. Clica em Votar de novo quando quiser.")
        )


class VotoModal(discord.ui.Modal, title="Urna"):
    numero = discord.ui.TextInput(label="Número do candidato", min_length=1, max_length=5)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        numero = self.numero.value.strip()
        candidatos = await self.cog.candidatos()

        if numero not in candidatos:
            await interaction.response.send_message(
                f"Ninguém concorre com o {numero}. Confere o número na lista e tenta de novo.", ephemeral=True
            )
            return

        try:
            candidato = await interaction.guild.fetch_member(candidatos[numero])
        except discord.NotFound:
            candidato = await interaction.client.fetch_user(candidatos[numero])

        await interaction.response.send_message(view=Confirmacao(self.cog, numero, candidato), ephemeral=True)


class Urna(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Votar", style=discord.ButtonStyle.blurple, custom_id="eleicao_votar")
    async def votar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog.aberta():
            await interaction.response.send_message("A votação já foi encerrada.", ephemeral=True)
            return
        if await self.cog.votantes.find_one({"_id": interaction.user.id}):
            await interaction.response.send_message("Seu voto já tá registrado.", ephemeral=True)
            return
        await interaction.response.send_modal(VotoModal(self.cog))


class Eleicao(commands.Cog):
    eleicao = app_commands.Group(
        name="eleicao",
        description="Eleição pra presidência do servidor",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot):
        self.bot = bot
        self.do_config = {
            str(c["numero"]): int(c["usuario"]) for c in bot.config.get("eleicao", {}).get("candidatos") or []
        }
        self.estado = bot.db["eleicao"]
        self.votantes = bot.db["eleicao_votantes"]
        self.cands = bot.db["eleicao_candidatos"]

    async def cog_load(self):
        self.bot.add_view(Urna(self))

    async def candidatos(self):
        extras = {d["_id"]: d["usuario"] async for d in self.cands.find()}
        return {**extras, **self.do_config}

    async def aberta(self):
        doc = await self.estado.find_one(ESTADO)
        return bool(doc and doc.get("aberta"))

    async def registrar_voto(self, user_id, numero):
        if not await self.aberta():
            return "fechada"
        try:
            await self.votantes.insert_one({"_id": user_id})
        except DuplicateKeyError:
            return "repetido"

        res = await self.estado.update_one({**ESTADO, "aberta": True}, {"$inc": {f"votos.{numero}": 1}})
        if not res.matched_count:
            await self.votantes.delete_one({"_id": user_id})
            return "fechada"
        return "ok"

    def embed_candidatos(self, candidatos, titulo):
        embed = discord.Embed(title=titulo, color=discord.Color.blurple())
        embed.description = "\n".join(
            f"**{n}** - <@{uid}>" for n, uid in sorted(candidatos.items(), key=lambda c: int(c[0]))
        )
        return embed

    @eleicao.command(name="registrar", description="Registra um candidato com o número dele")
    @app_commands.describe(numero="Número da candidatura", usuario="Menção ou ID do candidato")
    async def registrar(self, interaction: discord.Interaction, numero: str, usuario: discord.User):
        numero = numero.strip()
        if not numero.isdigit() or len(numero) > 5:
            await interaction.response.send_message("Número inválido, usa só dígitos (no máximo 5).", ephemeral=True)
            return
        if await self.aberta():
            await interaction.response.send_message(
                "Com a votação rolando não dá pra registrar ninguém.", ephemeral=True
            )
            return

        candidatos = await self.candidatos()
        if numero in candidatos:
            await interaction.response.send_message(
                f"O {numero} já tá com <@{candidatos[numero]}>, escolhe outro.", ephemeral=True
            )
            return
        if usuario.id in candidatos.values():
            await interaction.response.send_message(f"{usuario.mention} já é candidato.", ephemeral=True)
            return

        await self.cands.insert_one({"_id": numero, "usuario": usuario.id})
        await interaction.response.send_message(
            f"{usuario.mention} agora concorre com o número **{numero}**.", ephemeral=True
        )

    @eleicao.command(name="remover", description="Remove um candidato pelo número")
    async def remover(self, interaction: discord.Interaction, numero: str):
        numero = numero.strip()
        if await self.aberta():
            await interaction.response.send_message("Espera a votação acabar pra tirar alguém.", ephemeral=True)
            return
        if numero in self.do_config:
            await interaction.response.send_message(
                "Esse aí veio do config.yaml, tem que tirar de lá e reiniciar o bot.", ephemeral=True
            )
            return
        res = await self.cands.delete_one({"_id": numero})
        if not res.deleted_count:
            await interaction.response.send_message(f"Não achei ninguém com o {numero}.", ephemeral=True)
            return
        await interaction.response.send_message(f"Tirei o {numero} da disputa.", ephemeral=True)

    @eleicao.command(name="candidatos", description="Lista os candidatos registrados")
    async def listar(self, interaction: discord.Interaction):
        candidatos = await self.candidatos()
        if not candidatos:
            await interaction.response.send_message("Ainda não tem candidato nenhum.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self.embed_candidatos(candidatos, "Candidatos"), ephemeral=True)

    @eleicao.command(name="abrir", description="Abre a votação e manda a urna neste canal")
    async def abrir(self, interaction: discord.Interaction):
        candidatos = await self.candidatos()
        if len(candidatos) < 2:
            await interaction.response.send_message("Eleição com menos de 2 candidatos não rola.", ephemeral=True)
            return

        try:
            await self.estado.insert_one({**ESTADO, "aberta": True, "votos": {}})
        except DuplicateKeyError:
            await interaction.response.send_message(
                "Essa eleição já aconteceu, não dá pra abrir de novo.", ephemeral=True
            )
            return

        embed = self.embed_candidatos(candidatos, "Eleição para presidente do servidor")
        embed.description += (
            "\n\nPra votar é só clicar no botão aqui embaixo e digitar o número."
            "\nNinguém fica sabendo em quem você votou, nem a staff: só a contagem de cada um é salva."
        )
        await interaction.channel.send(embed=embed, view=Urna(self))
        await interaction.response.send_message("Urna no ar!", ephemeral=True)

    @eleicao.command(name="encerrar", description="Encerra a votação e mostra o resultado")
    async def encerrar(self, interaction: discord.Interaction):
        doc = await self.estado.find_one_and_update(
            {**ESTADO, "aberta": True}, {"$set": {"aberta": False}}, return_document=True
        )
        if not doc:
            await interaction.response.send_message("Não tem nenhuma votação rolando agora.", ephemeral=True)
            return

        candidatos = await self.candidatos()
        votos = {n: 0 for n in candidatos} | doc.get("votos", {})
        total = sum(votos.values())
        ranking = sorted(votos.items(), key=lambda v: (-v[1], int(v[0])))

        linhas = []
        for pos, (n, qtd) in enumerate(ranking, 1):
            pct = qtd / total * 100 if total else 0
            quem = f"<@{candidatos[n]}>" if n in candidatos else "(saiu da disputa)"
            linhas.append(f"`#{pos}` **{n}** - {quem}: **{qtd}** {'voto' if qtd == 1 else 'votos'} ({pct:.1f}%)")

        embed = discord.Embed(title="Resultado da eleição", description="\n".join(linhas), color=discord.Color.gold())
        embed.add_field(name="Total de votos", value=str(total))

        if total:
            topo = [n for n, q in ranking if q == ranking[0][1]]
            if len(topo) > 1:
                embed.add_field(name="Resultado", value="Empate entre " + ", ".join(f"**{n}**" for n in topo))
            else:
                n = topo[0]
                embed.add_field(name="Eleito", value=f"<@{candidatos[n]}>" if n in candidatos else f"**{n}**")

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Eleicao(bot))

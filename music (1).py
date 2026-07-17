"""
- /노래채널설정 : 관리자가 특정 텍스트채널을 '노래채널'로 지정
- 지정된 노래채널에 텍스트를 치면 해당 문자열로 곡을 검색해서 재생
- 재생 메시지에는 일시정지/다음곡/정지 버튼이 붙음 (music_view.py)
"""
import discord
from discord import app_commands
from discord.ext import commands
import wavelink

from song_channels import get_song_channel, set_song_channel, remove_song_channel
from music_view import MusicControlView


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- 노래채널 지정/해제 명령어 ----------

    @app_commands.command(name="노래채널설정", description="이 채널을 노래 요청 전용 채널로 지정합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_song_channel_cmd(self, interaction: discord.Interaction):
        set_song_channel(interaction.guild_id, interaction.channel_id)
        await interaction.response.send_message(
            f"✅ {interaction.channel.mention} 채널이 노래채널로 지정되었어요.\n"
            f"이제 이 채널에 노래 제목을 입력하면 바로 재생됩니다.",
        )

    @app_commands.command(name="노래채널해제", description="노래채널 지정을 해제합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_song_channel_cmd(self, interaction: discord.Interaction):
        remove_song_channel(interaction.guild_id)
        await interaction.response.send_message("🔕 노래채널 지정을 해제했어요.")

    @set_song_channel_cmd.error
    @remove_song_channel_cmd.error
    async def _perm_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("이 명령어는 관리자만 사용할 수 있어요.", ephemeral=True)
        else:
            raise error

    async def _search_track(self, query: str):
        """SoundCloud에서 먼저 검색하고, 결과가 없으면 유튜브로 폴백한다."""
        # 이미 URL을 직접 붙여넣은 경우엔 그대로 검색 (프리픽스 붙이지 않음)
        if query.startswith("http://") or query.startswith("https://"):
            try:
                return await wavelink.Playable.search(query)
            except Exception:
                return None

        # 1차: SoundCloud
        try:
            sc_tracks = await wavelink.Playable.search(query, source=wavelink.TrackSource.SoundCloud)
        except Exception:
            sc_tracks = None

        if sc_tracks:
            return sc_tracks

        # 2차: 유튜브 (SoundCloud에 없을 때만 시도, 실패할 수 있음)
        try:
            yt_tracks = await wavelink.Playable.search(query)
        except Exception:
            yt_tracks = None

        return yt_tracks

    # ---------- 노래채널에서의 메시지 → 재생 트리거 ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        song_channel_id = get_song_channel(message.guild.id)
        if song_channel_id is None or message.channel.id != song_channel_id:
            return

        query = message.content.strip()
        if not query:
            return

        # 요청자가 음성채널에 있는지 확인
        if not isinstance(message.author, discord.Member) or message.author.voice is None:
            await message.channel.send(
                f"{message.author.mention} 먼저 음성채널에 들어온 후 노래 제목을 입력해 주세요.",
                delete_after=8,
            )
            return

        voice_channel = message.author.voice.channel

        try:
            await message.add_reaction("🔎")
        except discord.HTTPException:
            pass

        # 검색: SoundCloud 우선, 없으면 유튜브로 폴백
        tracks = await self._search_track(query)

        if not tracks:
            await message.channel.send(f"❌ '{query}'에 대한 검색 결과를 찾지 못했어요.", delete_after=8)
            return

        track = tracks[0] if not isinstance(tracks, wavelink.Playlist) else tracks.tracks[0]

        # 플레이어 연결 (없으면 새로 접속)
        player: wavelink.Player = message.guild.voice_client  # type: ignore
        if player is None:
            try:
                player = await voice_channel.connect(cls=wavelink.Player)
            except discord.ClientException:
                await message.channel.send("음성채널 연결에 실패했어요. 잠시 후 다시 시도해 주세요.")
                return
        elif player.channel.id != voice_channel.id:
            await message.channel.send(
                f"봇이 이미 다른 음성채널({player.channel.mention})에서 재생 중이에요.", delete_after=8
            )
            return

        player.autoplay = wavelink.AutoPlayMode.partial

        if player.playing or player.paused:
            await player.queue.put_wait(track)
            await message.channel.send(f"➕ 대기열에 추가됨: **{track.title}**")
        else:
            await player.play(track)

    # ---------- 트랙 재생 시작 시 → 컨트롤 버튼 포함된 안내 메시지 전송 ----------

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player = payload.player
        if player is None:
            return

        track = payload.track
        channel_id = get_song_channel(player.guild.id)
        channel = player.guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return

        embed = discord.Embed(
            title="🎵 지금 재생 중",
            description=f"**{track.title}**\n{track.author}",
            color=discord.Color.blurple(),
        )
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        minutes, seconds = divmod(track.length // 1000, 60)
        embed.add_field(name="길이", value=f"{minutes:02d}:{seconds:02d}")

        view = MusicControlView(player)
        await channel.send(embed=embed, view=view)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player):
        # 오래 아무 활동 없으면 자동 퇴장
        try:
            await player.disconnect()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))

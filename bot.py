import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import yt_dlp
import aiohttp

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN')
LASTFM_API_KEY = os.environ.get('LASTFM_API_KEY', 'YOUR_LASTFM_API_KEY')
LASTFM_API_URL = 'http://ws.audioscrobbler.com/2.0/'
DB_PATH = 'users.db'

# Ad messages
AD_MESSAGES = [
    "Реклама: Попробуй наш партнёрский бот @CoolMusicBot!",
    "Реклама: Открой новую музыку с @DiscoverMusicBot!",
    "Реклама: Слушай подкасты на @PodcastHubBot!",
]


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                mode TEXT DEFAULT 'basic',
                interaction_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                track_name TEXT,
                artist TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS preferences (
                user_id INTEGER PRIMARY KEY,
                favorite_genres TEXT,
                favorite_artists TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        conn.commit()
        conn.close()

    def get_user(self, user_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = cursor.fetchone()
        conn.close()
        return user

    def create_user(self, user_id: int, username: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)',
            (user_id, username)
        )
        conn.commit()
        conn.close()

    def update_mode(self, user_id: int, mode: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET mode = ? WHERE user_id = ?', (mode, user_id))
        conn.commit()
        conn.close()

    def increment_interaction(self, user_id: int) -> int:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE users SET interaction_count = interaction_count + 1 WHERE user_id = ?',
            (user_id,)
        )
        cursor.execute('SELECT interaction_count FROM users WHERE user_id = ?', (user_id,))
        count = cursor.fetchone()[0]
        conn.commit()
        conn.close()
        return count

    def add_download(self, user_id: int, track_name: str, artist: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO downloads (user_id, track_name, artist) VALUES (?, ?, ?)',
            (user_id, track_name, artist)
        )
        conn.commit()
        conn.close()

    def get_user_history(self, user_id: int, limit: int = 10):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT track_name, artist FROM downloads WHERE user_id = ? ORDER BY downloaded_at DESC LIMIT ?',
            (user_id, limit)
        )
        history = cursor.fetchall()
        conn.close()
        return history


db = Database(DB_PATH)


class LastFMClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = None

    async def get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session

    async def search_track(self, query: str, limit: int = 5):
        session = await self.get_session()
        params = {
            'method': 'track.search',
            'track': query,
            'api_key': self.api_key,
            'format': 'json',
            'limit': limit
        }
        try:
            async with session.get(LASTFM_API_URL, params=params) as response:
                data = await response.json()
                if 'results' in data and 'trackmatches' in data['results']:
                    tracks = data['results']['trackmatches'].get('track', [])
                    if isinstance(tracks, dict):
                        tracks = [tracks]
                    return tracks
        except Exception as e:
            logger.error(f"LastFM search error: {e}")
        return []

    async def get_similar_tracks(self, artist: str, track: str, limit: int = 5):
        session = await self.get_session()
        params = {
            'method': 'track.getSimilar',
            'artist': artist,
            'track': track,
            'api_key': self.api_key,
            'format': 'json',
            'limit': limit
        }
        try:
            async with session.get(LASTFM_API_URL, params=params) as response:
                data = await response.json()
                if 'similartracks' in data and 'track' in data['similartracks']:
                    tracks = data['similartracks']['track']
                    if isinstance(tracks, dict):
                        tracks = [tracks]
                    return tracks
        except Exception as e:
            logger.error(f"LastFM similar tracks error: {e}")
        return []

    async def get_top_tracks(self, limit: int = 10):
        session = await self.get_session()
        params = {
            'method': 'chart.getTopTracks',
            'api_key': self.api_key,
            'format': 'json',
            'limit': limit
        }
        try:
            async with session.get(LASTFM_API_URL, params=params) as response:
                data = await response.json()
                if 'tracks' in data and 'track' in data['tracks']:
                    return data['tracks']['track']
        except Exception as e:
            logger.error(f"LastFM top tracks error: {e}")
        return []

    async def close(self):
        if self.session:
            await self.session.close()


lastfm = LastFMClient(LASTFM_API_KEY)


async def download_audio(query: str) -> Optional[str]:
    """Download audio using yt-dlp"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': '/tmp/%(title)s.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'default_search': 'ytsearch1',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=True)
            if info and 'entries' in info:
                info = info['entries'][0]

            filename = ydl.prepare_filename(info)
            base_filename = filename.rsplit('.', 1)[0]
            mp3_filename = f"{base_filename}.mp3"

            return mp3_filename
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None


def should_show_ad(interaction_count: int) -> bool:
    """Check if ad should be shown (every 10 interactions)"""
    return interaction_count > 0 and interaction_count % 10 == 0


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    db.create_user(user.id, user.username or user.first_name)

    keyboard = [
        [InlineKeyboardButton("🎵 Базовый режим", callback_data='mode_basic')],
        [InlineKeyboardButton("🎼 Расширенный режим", callback_data='mode_advanced')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = (
        f"👋 Привет, {user.first_name}!\n\n"
        "Добро пожаловать в MelodyForge — твой музыкальный помощник!\n\n"
        "🎵 *Базовый режим*: Поиск и скачивание музыки\n"
        "🎼 *Расширенный режим*: Рекомендации, плейлисты и миксы\n\n"
        "Выбери режим работы:"
    )

    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == 'mode_basic':
        db.update_mode(user_id, 'basic')
        text = (
            "🎵 *Базовый режим активирован*\n\n"
            "Отправь мне название песни или исполнителя, "
            "и я найду и скачаю музыку для тебя!\n\n"
            "Например: `Imagine Dragons Believer`"
        )
        await query.edit_message_text(text, parse_mode='Markdown')

    elif data == 'mode_advanced':
        db.update_mode(user_id, 'advanced')
        keyboard = [
            [InlineKeyboardButton("🔍 Поиск музыки", callback_data='adv_search')],
            [InlineKeyboardButton("💡 Рекомендации", callback_data='adv_recommendations')],
            [InlineKeyboardButton("🎧 Популярные треки", callback_data='adv_top_tracks')],
            [InlineKeyboardButton("📜 Моя история", callback_data='adv_history')],
            [InlineKeyboardButton("🔙 Назад", callback_data='back_to_start')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (
            "🎼 *Расширенный режим активирован*\n\n"
            "Выбери действие:"
        )
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    elif data == 'adv_search':
        text = (
            "🔍 *Поиск музыки*\n\n"
            "Отправь название песни или исполнителя, и я найду лучшие совпадения!\n\n"
            "Например: `The Beatles Yesterday`"
        )
        await query.edit_message_text(text, parse_mode='Markdown')

    elif data == 'adv_recommendations':
        text = (
            "💡 *Получить рекомендации*\n\n"
            "Отправь название любимого трека в формате:\n"
            "`/similar Исполнитель - Название`\n\n"
            "Например: `/similar Coldplay - Fix You`"
        )
        await query.edit_message_text(text, parse_mode='Markdown')

    elif data == 'adv_top_tracks':
        await query.edit_message_text("⏳ Загружаю популярные треки...")
        tracks = await lastfm.get_top_tracks(limit=10)

        if tracks:
            text = "🎧 *Топ-10 треков сегодня:*\n\n"
            for i, track in enumerate(tracks, 1):
                artist = track.get('artist', {}).get('name', 'Unknown')
                name = track.get('name', 'Unknown')
                text += f"{i}. {artist} - {name}\n"
            text += "\nОтправь название, чтобы скачать!"
        else:
            text = "❌ Не удалось загрузить топ треков. Попробуй позже."

        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='mode_advanced')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    elif data == 'adv_history':
        history = db.get_user_history(user_id, limit=10)

        if history:
            text = "📜 *Твоя история скачиваний:*\n\n"
            for i, (track, artist) in enumerate(history, 1):
                text += f"{i}. {artist} - {track}\n"
        else:
            text = "📜 *История пуста*\n\nСкачай свой первый трек!"

        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='mode_advanced')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    elif data == 'back_to_start':
        keyboard = [
            [InlineKeyboardButton("🎵 Базовый режим", callback_data='mode_basic')],
            [InlineKeyboardButton("🎼 Расширенный режим", callback_data='mode_advanced')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (
            "Выбери режим работы:\n\n"
            "🎵 *Базовый режим*: Поиск и скачивание музыки\n"
            "🎼 *Расширенный режим*: Рекомендации, плейлисты и миксы"
        )
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')


async def similar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /similar command for recommendations"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "💡 Используй: `/similar Исполнитель - Название`\n\n"
            "Например: `/similar Radiohead - Creep`",
            parse_mode='Markdown'
        )
        return

    query = ' '.join(context.args)

    if '-' in query:
        artist, track = query.split('-', 1)
        artist = artist.strip()
        track = track.strip()
    else:
        await update.message.reply_text(
            "❌ Неверный формат. Используй: `/similar Исполнитель - Название`",
            parse_mode='Markdown'
        )
        return

    await update.message.reply_text("⏳ Ищу похожие треки...")

    similar_tracks = await lastfm.get_similar_tracks(artist, track, limit=8)

    if similar_tracks:
        text = f"💡 *Похожие на {artist} - {track}:*\n\n"
        for i, similar in enumerate(similar_tracks, 1):
            s_artist = similar.get('artist', {}).get('name', 'Unknown')
            s_name = similar.get('name', 'Unknown')
            text += f"{i}. {s_artist} - {s_name}\n"
        text += "\nОтправь название, чтобы скачать!"
        await update.message.reply_text(text, parse_mode='Markdown')
    else:
        await update.message.reply_text(
            "❌ Не удалось найти похожие треки. Проверь правильность названия."
        )

    # Increment interaction and check for ad
    count = db.increment_interaction(user_id)
    if should_show_ad(count):
        ad_message = random.choice(AD_MESSAGES)
        await update.message.reply_text(ad_message)


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (search queries)"""
    user_id = update.effective_user.id
    query = update.message.text

    user = db.get_user(user_id)
    if not user:
        db.create_user(user_id, update.effective_user.username or update.effective_user.first_name)
        user = db.get_user(user_id)

    mode = user[2] if user else 'basic'

    # Increment interaction
    count = db.increment_interaction(user_id)

    await update.message.reply_text("🔍 Ищу музыку...")

    if mode == 'advanced':
        # Search using Last.fm
        tracks = await lastfm.search_track(query, limit=5)

        if tracks:
            text = "🎵 *Результаты поиска:*\n\n"
            for i, track in enumerate(tracks, 1):
                artist = track.get('artist', 'Unknown')
                name = track.get('name', 'Unknown')
                text += f"{i}. {artist} - {name}\n"
            text += "\nСкачиваю первый результат..."
            await update.message.reply_text(text, parse_mode='Markdown')

            # Download first result
            first_track = tracks[0]
            artist = first_track.get('artist', 'Unknown')
            name = first_track.get('name', 'Unknown')
            download_query = f"{artist} {name}"
        else:
            download_query = query
    else:
        download_query = query

    # Download
    await update.message.reply_text("⬇️ Скачиваю...")

    file_path = await download_audio(download_query)

    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, 'rb') as audio_file:
                await update.message.reply_audio(
                    audio=audio_file,
                    title=download_query,
                    performer="MelodyForge"
                )

            # Save to history
            parts = download_query.split(' ', 1)
            artist = parts[0] if len(parts) > 0 else "Unknown"
            track = parts[1] if len(parts) > 1 else download_query
            db.add_download(user_id, track, artist)

            # Clean up
            os.remove(file_path)
        except Exception as e:
            logger.error(f"Error sending audio: {e}")
            await update.message.reply_text(
                "❌ Ошибка при отправке файла. Попробуй другой запрос."
            )
    else:
        await update.message.reply_text(
            "❌ Не удалось скачать трек. Попробуй уточнить запрос."
        )

    # Show ad if needed
    if should_show_ad(count):
        ad_message = random.choice(AD_MESSAGES)
        await update.message.reply_text(ad_message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = (
        "🎵 *MelodyForge - Помощь*\n\n"
        "*Команды:*\n"
        "/start - Главное меню\n"
        "/similar Исполнитель - Название - Похожие треки\n"
        "/help - Эта справка\n\n"
        "*Как пользоваться:*\n"
        "1. Выбери режим работы\n"
        "2. Отправь название песни или исполнителя\n"
        "3. Получи музыку!\n\n"
        "*Расширенный режим:*\n"
        "- Рекомендации на основе твоих предпочтений\n"
        "- Популярные треки\n"
        "- История скачиваний\n"
        "- Похожие треки"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')


def main():
    """Start the bot"""
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("similar", similar_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Start bot
    logger.info("Starting MelodyForge bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    try:
        main()
    finally:
        asyncio.run(lastfm.close())

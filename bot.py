import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram.ext import Application, MessageHandler, filters
from telegram import Update
import sqlite3
import hashlib
from datetime import datetime, timedelta
import signal
import sys
import pytz

# Setup logging
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# Set timezone untuk logging
logging.Formatter.converter = lambda *args: datetime.now(pytz.timezone('Asia/Jakarta')).timetuple()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

class ProductionDuplicateBot:
    def __init__(self):
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
        load_dotenv(env_path)
        self.token = os.getenv('BOT_TOKEN')
        if not self.token:
            raise ValueError("❌ BOT_TOKEN environment variable not set")
            
        # Set zona waktu Jakarta
        self.timezone = pytz.timezone('Asia/Jakarta')
            
        self.app = Application.builder().token(self.token).build()
        self.setup_database()
        self.setup_handlers()
        self.setup_error_handler()
        self.setup_signal_handlers()
        
        logger.info("🤖 Bot initialized successfully with Jakarta timezone")
        
    def setup_database(self):
        """Setup database dengan path yang lebih baik"""
        db_path = os.getenv('DB_PATH', 'messages.db')
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                message_hash TEXT,
                message_text TEXT,
                user_id INTEGER,
                timestamp DATETIME,
                UNIQUE(chat_id, message_hash)
            )
        ''')
        
        # Migrasi: Tambahkan kolom user_name jika belum ada
        try:
            cursor.execute('ALTER TABLE messages ADD COLUMN user_name TEXT DEFAULT "Unknown"')
        except sqlite3.OperationalError:
            pass # Kolom sudah ada
            
        self.conn.commit()
        logger.info(f"📊 Database initialized at: {db_path}")
        
    def setup_handlers(self):
        """Setup handler untuk pesan teks"""
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
    def setup_error_handler(self):
        """Handle errors untuk production"""
        async def error_handler(update: Update, context):
            logger.error(f"Error: {context.error}")
            
        self.app.add_error_handler(error_handler)
        
    def setup_signal_handlers(self):
        """Handle shutdown signals"""
        def signal_handler(signum, frame):
            logger.info("🛑 Received shutdown signal")
            asyncio.create_task(self.graceful_shutdown())
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
    async def graceful_shutdown(self):
        """Shutdown yang graceful"""
        logger.info("🔚 Shutting down gracefully...")
        self.conn.close()
        await self.app.shutdown()
        sys.exit(0)
        
    def generate_message_hash(self, text):
        """Generate hash untuk pesan untuk deteksi duplikat"""
        normalized_text = ' '.join(text.lower().split())
        return hashlib.md5(normalized_text.encode()).hexdigest()
        
    def get_current_time(self):
        """Mendapatkan waktu saat ini dalam zona waktu Jakarta"""
        return datetime.now(self.timezone)
    
    def format_time_for_db(self, dt=None):
        """Format waktu untuk penyimpanan di database"""
        if dt is None:
            dt = self.get_current_time()
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    
    def format_time_display(self, dt_str):
        """Format waktu untuk ditampilkan ke user"""
        try:
            # Parse waktu dari database
            dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
            # Pastikan waktu memiliki timezone Jakarta
            if dt.tzinfo is None:
                dt = self.timezone.localize(dt)
            return dt.strftime('%Y/%m/%d %H:%M:%S')
        except Exception as e:
            logger.error(f"Error formatting time: {e}")
            return dt_str
        
    async def handle_message(self, update: Update, context):
        """Handle incoming messages"""
        try:
            message = update.message
            if not message or not message.text:
                return
                
            chat_id = message.chat_id
            user_id = message.from_user.id
            user_name = message.from_user.first_name if message.from_user.first_name else str(user_id)
            message_text = message.text
            
            # Skip jika pesan terlalu pendek (dikurangi jadi 5 agar fitur No HP terdeteksi)
            if len(message_text.strip()) < 5:  
                return
                
            message_hash = self.generate_message_hash(message_text)
            
            cursor = self.conn.cursor()
            
            # Cek apakah pesan sudah pernah dikirim dalam 24 jam terakhir
            cursor.execute('''
                SELECT user_id, message_text, timestamp, user_name 
                FROM messages 
                WHERE chat_id = ? AND message_hash = ? 
                AND timestamp > datetime('now', '-1 day')
            ''', (chat_id, message_hash))
            
            existing_message = cursor.fetchone()
            
            if existing_message:
                original_user_id, original_text, original_time, original_user_name = existing_message
                
                # Format waktu untuk ditampilkan
                original_time_str = self.format_time_display(original_time)
                current_time_str = self.format_time_display(self.format_time_for_db())
                
                response_message = (
                    f"❌DETEKSI SISTEM❌\n"
                    f"TEXT: {original_text}\n"
                    f"{original_user_name} : {original_time_str} (pertama kali)\n"
                    f"{user_name} : {current_time_str} (kali ini)"
                )
                
                msg = await message.reply_text(response_message)
                logger.info(f"🚫 Duplicate detected in chat {chat_id} at {current_time_str} WIB")
            else:
                # Simpan pesan baru ke database dengan waktu Jakarta
                current_time = self.format_time_for_db()
                
                cursor.execute('''
                    INSERT OR REPLACE INTO messages 
                    (chat_id, message_hash, message_text, user_id, timestamp, user_name)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (chat_id, message_hash, message_text, user_id, current_time, user_name))
                self.conn.commit()
                
                logger.info(f"✅ New message saved at {current_time} WIB")
                
            # Bersihkan pesan yang lebih dari 7 hari
            cursor.execute('DELETE FROM messages WHERE timestamp < datetime("now", "-7 days")')
            self.conn.commit()
            
        except Exception as e:
            logger.error(f"Error handling message: {e}")
        
    def run_polling(self):
        """Jalankan dengan polling (untuk development)"""
        current_time = self.format_time_display(self.format_time_for_db())
        logger.info(f"🔄 Starting bot with polling at {current_time} WIB...")
        self.app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        
    def run_webhook(self):
        """Jalankan dengan webhook (untuk production)"""
        webhook_url = os.getenv('WEBHOOK_URL')
        port = int(os.getenv('PORT', 8080))
        
        if not webhook_url:
            logger.warning("⚠️ WEBHOOK_URL not set, falling back to polling")
            return self.run_polling()
            
        current_time = self.format_time_display(self.format_time_for_db())
        logger.info(f"🌐 Starting bot with webhook: {webhook_url} at {current_time} WIB")
        self.app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=self.token,
            webhook_url=f"{webhook_url}/{self.token}",
            drop_pending_updates=True
        )

if __name__ == "__main__":
    try:
        bot = ProductionDuplicateBot()
        
        # Pilih mode berdasarkan environment
        if os.getenv('USE_WEBHOOK', 'false').lower() == 'true':
            bot.run_webhook()
        else:
            bot.run_polling()
            
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")
        sys.exit(1)

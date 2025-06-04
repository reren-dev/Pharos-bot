
import asyncio
from flask import Flask
from threading import Thread

# Simple web server to keep Replit alive
app = Flask('')

@app.route('/')
def home():
    return "Pharos Bot is running!"

def run_web():
    app.run(host='0.0.0.0', port=8080)

# Jalankan server web di thread terpisah
Thread(target=run_web).start()
import json
import logging
import sqlite3
import os
from datetime import datetime
from typing import Dict, Set, Optional
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import web3
from web3 import Web3

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Reduce noisy logging from some libraries
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
RPC_URL = "https://testnet.dplabs-internal.com/"
EXPLORER_URL = "https://https://pharos-testnet.socialscan.io/"
CHAIN_ID = 688688

# Database setup
def init_database():
    """Initialize SQLite database for storing user data"""
    conn = sqlite3.connect('pharos_bot.db')
    cursor = conn.cursor()
    
    # Create users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            wallet_address TEXT UNIQUE,
            last_block_checked INTEGER DEFAULT 0,
            is_group_member BOOLEAN DEFAULT 0,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create transactions table for tracking
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tracked_transactions (
            tx_hash TEXT PRIMARY KEY,
            user_id INTEGER,
            block_number INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    
    conn.commit()
    conn.close()

class PharosMonitor:
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        # For PoA networks, we can use the newer ExtraDataToPOAMiddleware or skip if not needed
        try:
            from web3.middleware import ExtraDataToPOAMiddleware
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        except ImportError:
            # If middleware is not available, continue without it
            logger.warning("PoA middleware not available, continuing without it")
        
        self.monitored_addresses: Dict[str, int] = {}  # address -> user_id
        self.last_checked_block = 0
        
    async def get_latest_block(self) -> int:
        """Get the latest block number"""
        try:
            # Use async approach for better performance
            latest = self.w3.eth.block_number
            logger.debug(f"Latest block: {latest}")
            return latest
        except Exception as e:
            logger.error(f"Error getting latest block: {e}")
            return self.last_checked_block
    
    async def check_transactions_in_block(self, block_number: int) -> list:
        """Check for transactions involving monitored addresses in a specific block"""
        found_transactions = []
        
        try:
            block = self.w3.eth.get_block(block_number, full_transactions=True)
            
            for tx in block.transactions:
                # Check if transaction involves any monitored address
                from_addr = tx['from'].lower() if tx['from'] else None
                to_addr = tx['to'].lower() if tx['to'] else None
                
                for monitored_addr, user_id in self.monitored_addresses.items():
                    monitored_addr_lower = monitored_addr.lower()
                    
                    if from_addr == monitored_addr_lower or to_addr == monitored_addr_lower:
                        tx_type = "outgoing" if from_addr == monitored_addr_lower else "incoming"
                        
                        found_transactions.append({
                            'user_id': user_id,
                            'tx_hash': tx['hash'].hex(),
                            'from': tx['from'],
                            'to': tx['to'],
                            'value': self.w3.from_wei(tx['value'], 'ether'),
                            'type': tx_type,
                            'block_number': block_number,
                            'gas_used': tx['gas']
                        })
                        
        except Exception as e:
            logger.error(f"Error checking block {block_number}: {e}")
            
        return found_transactions

class TelegramBot:
    def __init__(self):
        self.application = None
        self.pharos_monitor = PharosMonitor()
        
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name
        
        welcome_text = (
            "🔥 *Selamat datang di Pharos Testnet Transaction Monitor Bot!* 🔥\n\n"
            "Bot ini akan memantau transaksi masuk dan keluar dari alamat wallet Anda di Pharos Testnet.\n\n"
            "*Syarat penggunaan:*\n"
            "1. Anda harus bergabung dengan grup kami terlebih dahulu\n"
            "2. Setelah bergabung, Anda dapat mendaftarkan alamat wallet\n"
            "3. Alamat wallet bisa diganti kapan saja dengan register ulang\n"
            "4. Bot akan mengirim notifikasi real-time untuk setiap transaksi\n\n"
            "*Perintah yang tersedia:*\n"
            "• `/register 0x....address kamu` - Daftarkan alamat wallet\n"
            "• `/status` - Lihat status pendaftaran\n"
            "• `/unregister` - Hapus alamat wallet yang terdaftar\n"
            "• `/help` - Bantuan\n\n"
            "Silakan bergabung dengan grup kami terlebih dahulu!"
        )
        
        # Use the correct group link
        keyboard = [[InlineKeyboardButton("📢 Join Group", url="https://t.me/gbpharos")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=reply_markup)
        
        # Store user info
        self.store_user_info(user_id, username)
    
    async def register_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle wallet registration"""
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name
        
        # Check if user is in group
        is_member = await self.check_group_membership(user_id, context)
        if not is_member:
            await update.message.reply_text(
                "❌ Anda harus bergabung dengan grup kami terlebih dahulu sebelum mendaftar!\n"
                "Join grup: https://t.me/gbpharos\n\n"
                "💡 *Troubleshooting:*\n"
                "• Pastikan Anda sudah join grup dengan akun yang sama\n"
                "• Tunggu beberapa menit setelah join, lalu coba lagi\n"
                "• Jika masih bermasalah, hubungi admin dengan menyebutkan User ID Anda: `{}`".format(user_id),
                parse_mode='Markdown'
            )
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ Format salah! Gunakan: /register <alamat_wallet>\n"
                "Contoh: /register 0x1234567890123456789012345678901234567890"
            )
            return
        
        wallet_address = context.args[0]
        
        # Validate wallet address
        if not self.is_valid_address(wallet_address):
            await update.message.reply_text("❌ Alamat wallet tidak valid!")
            return
        
        # Register wallet
        success, status = self.register_wallet(user_id, wallet_address)
        
        if success:
            self.pharos_monitor.monitored_addresses[wallet_address] = user_id
            
            if status == "replaced":
                await update.message.reply_text(
                    f"✅ Alamat wallet berhasil diperbarui!\n"
                    f"👤 User: {username}\n"
                    f"📍 Alamat Baru: `{wallet_address}`\n"
                    f"🔗 Explorer: {EXPLORER_URL}address/{wallet_address}\n\n"
                    "🔄 Monitoring alamat lama telah dihentikan.\n"
                    "Bot sekarang akan memantau alamat baru secara real-time!",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    f"✅ Alamat wallet berhasil didaftarkan!\n"
                    f"👤 User: {username}\n"
                    f"📍 Alamat: `{wallet_address}`\n"
                    f"🔗 Explorer: {EXPLORER_URL}address/{wallet_address}\n\n"
                    "Bot sekarang akan memantau transaksi pada alamat ini secara real-time!",
                    parse_mode='Markdown'
                )
        else:
            if status == "wallet_taken":
                await update.message.reply_text(
                    "❌ Alamat wallet ini sudah didaftarkan oleh pengguna lain!\n"
                    "Setiap alamat wallet hanya bisa digunakan oleh satu pengguna."
                )
            else:
                await update.message.reply_text(
                    "❌ Gagal mendaftarkan alamat wallet. Silakan coba lagi."
                )
    
    async def force_register_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Force register wallet without group check (for troubleshooting)"""
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name
        
        # This command should only be used by admins for troubleshooting
        # You can add admin check here if needed
        
        if not context.args:
            await update.message.reply_text(
                "❌ Format salah! Gunakan: /forceregister <alamat_wallet>\n"
                "Contoh: /forceregister 0x1234567890123456789012345678901234567890\n\n"
                "⚠️ Perintah ini melewati pengecekan grup untuk troubleshooting."
            )
            return
        
        wallet_address = context.args[0]
        
        # Validate wallet address
        if not self.is_valid_address(wallet_address):
            await update.message.reply_text("❌ Alamat wallet tidak valid!")
            return
        
        # Force register wallet
        success, status = self.register_wallet(user_id, wallet_address)
        
        if success:
            self.pharos_monitor.monitored_addresses[wallet_address] = user_id
            
            if status == "replaced":
                await update.message.reply_text(
                    f"✅ Alamat wallet berhasil diperbarui (force register)!\n"
                    f"👤 User: {username}\n"
                    f"📍 Alamat Baru: `{wallet_address}`\n"
                    f"🔗 Explorer: {EXPLORER_URL}address/{wallet_address}\n\n"
                    "🔄 Monitoring alamat lama telah dihentikan.\n"
                    "Bot sekarang akan memantau alamat baru secara real-time!",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    f"✅ Alamat wallet berhasil didaftarkan (force register)!\n"
                    f"👤 User: {username}\n"
                    f"📍 Alamat: `{wallet_address}`\n"
                    f"🔗 Explorer: {EXPLORER_URL}address/{wallet_address}\n\n"
                    "Bot sekarang akan memantau transaksi pada alamat ini secara real-time!",
                    parse_mode='Markdown'
                )
        else:
            if status == "wallet_taken":
                await update.message.reply_text(
                    "❌ Alamat wallet ini sudah didaftarkan oleh pengguna lain!\n"
                    "Setiap alamat wallet hanya bisa digunakan oleh satu pengguna."
                )
            else:
                await update.message.reply_text(
                    "❌ Gagal mendaftarkan alamat wallet. Silakan coba lagi."
                )
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user registration status"""
        user_id = update.effective_user.id
        
        conn = sqlite3.connect('pharos_bot.db')
        cursor = conn.cursor()
        cursor.execute('SELECT wallet_address, is_group_member, registered_at FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            await update.message.reply_text("❌ Anda belum terdaftar. Gunakan /start untuk memulai.")
            return
        
        wallet_address, is_group_member, registered_at = result
        
        status_text = f"📊 *Status Pendaftaran Anda:*\n\n"
        status_text += f"👤 User ID: `{user_id}`\n"
        status_text += f"👥 Member Grup: {'✅ Ya' if is_group_member else '❌ Tidak'}\n"
        
        if wallet_address:
            status_text += f"💼 Wallet: `{wallet_address}`\n"
            status_text += f"🔗 Explorer: {EXPLORER_URL}address/{wallet_address}\n"
            status_text += f"📅 Terdaftar: {registered_at}\n"
            status_text += f"🔄 Status Monitoring: {'🟢 Aktif' if wallet_address in self.pharos_monitor.monitored_addresses else '🔴 Tidak Aktif'}"
        else:
            status_text += "💼 Wallet: Belum didaftarkan"
        
        await update.message.reply_text(status_text, parse_mode='Markdown')
    
    async def unregister_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Unregister wallet address"""
        user_id = update.effective_user.id
        
        conn = sqlite3.connect('pharos_bot.db')
        cursor = conn.cursor()
        cursor.execute('SELECT wallet_address FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result or not result[0]:
            await update.message.reply_text("❌ Anda tidak memiliki alamat wallet yang terdaftar.")
            conn.close()
            return
        
        wallet_address = result[0]
        
        # Remove from database
        cursor.execute('UPDATE users SET wallet_address = NULL WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        
        # Remove from monitoring
        if wallet_address in self.pharos_monitor.monitored_addresses:
            del self.pharos_monitor.monitored_addresses[wallet_address]
        
        await update.message.reply_text(
            f"✅ Alamat wallet `{wallet_address}` berhasil dihapus dari monitoring!"
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help information"""
        help_text = (
            "🆘 *Bantuan Pharos Monitor Bot*\n\n"
            "*Perintah yang tersedia:*\n"
            "• `/start` - Memulai bot dan melihat informasi\n"
            "• `/register <address>` - Mendaftarkan alamat wallet\n"
            "• `/status` - Melihat status pendaftaran\n"
            "• `/unregister` - Menghapus alamat wallet\n"
            "• `/help` - Menampilkan bantuan ini\n\n"
            "*Informasi Jaringan:*\n"
            f"• Chain ID: {CHAIN_ID}\n"
            f"• RPC URL: {RPC_URL}\n"
            f"• Explorer: {EXPLORER_URL}\n\n"
            "*Catatan:*\n"
            "- Anda harus bergabung grup terlebih dahulu\n"
            "- Alamat wallet bisa diganti kapan saja\n"
            "- Setiap alamat hanya bisa digunakan oleh 1 user\n"
            "- Notifikasi real-time untuk semua transaksi"
        )
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    def store_user_info(self, user_id: int, username: str):
        """Store user information in database"""
        conn = sqlite3.connect('pharos_bot.db')
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)',
            (user_id, username)
        )
        conn.commit()
        conn.close()
    
    async def check_group_membership(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Check if user is member of the required group"""
        try:
            logger.info(f"Checking membership for user {user_id} in group {GROUP_CHAT_ID}")
            chat_member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
            
            logger.info(f"User {user_id} status in group: {chat_member.status}")
            
            # Include 'restricted' status as well, since some groups have restrictions
            is_member = chat_member.status in ['member', 'administrator', 'creator', 'restricted']
            
            # Update database
            conn = sqlite3.connect('pharos_bot.db')
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET is_group_member = ? WHERE user_id = ?', (is_member, user_id))
            conn.commit()
            conn.close()
            
            logger.info(f"User {user_id} membership status: {'✅ Member' if is_member else '❌ Not member'}")
            return is_member
            
        except Exception as e:
            logger.error(f"Error checking group membership for user {user_id}: {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            # In case of error, we'll assume they might be a member and let them try
            # This prevents false negatives due to API issues
            return True
    
    def is_valid_address(self, address: str) -> bool:
        """Validate Ethereum address"""
        try:
            return Web3.is_address(address)
        except:
            return False
    
    def register_wallet(self, user_id: int, wallet_address: str) -> tuple[bool, str]:
        """Register wallet address for user"""
        try:
            conn = sqlite3.connect('pharos_bot.db')
            cursor = conn.cursor()
            
            # Check if wallet is already registered by another user
            cursor.execute('SELECT user_id FROM users WHERE wallet_address = ? AND user_id != ?', 
                         (wallet_address, user_id))
            other_user = cursor.fetchone()
            
            if other_user:
                conn.close()
                return False, "wallet_taken"
            
            # Check if user already has a wallet registered
            cursor.execute('SELECT wallet_address FROM users WHERE user_id = ?', (user_id,))
            current_wallet = cursor.fetchone()
            
            # Remove old wallet from monitoring if exists
            if current_wallet and current_wallet[0]:
                old_wallet = current_wallet[0]
                if old_wallet in self.pharos_monitor.monitored_addresses:
                    del self.pharos_monitor.monitored_addresses[old_wallet]
            
            # Update with new wallet
            cursor.execute('UPDATE users SET wallet_address = ? WHERE user_id = ?', 
                         (wallet_address, user_id))
            conn.commit()
            conn.close()
            
            if current_wallet and current_wallet[0]:
                return True, "replaced"
            else:
                return True, "new"
                
        except Exception as e:
            logger.error(f"Error registering wallet: {e}")
            return False, "error"
    
    async def send_transaction_notification(self, user_id: int, tx_data: dict):
        """Send transaction notification to user"""
        try:
            tx_type_emoji = "📤" if tx_data['type'] == "outgoing" else "📥"
            tx_type_text = "Keluar" if tx_data['type'] == "outgoing" else "Masuk"
            
            notification_text = (
                f"{tx_type_emoji} *Transaksi {tx_type_text} Terdeteksi!*\n\n"
                f"💰 *Jumlah:* {tx_data['value']:.6f} PHRS\n"
                f"📤 *Dari:* `{tx_data['from']}`\n\n"
                f"📥 *Ke:* `{tx_data['to']}`\n"
            )
            
            await self.application.bot.send_message(
                chat_id=user_id,
                text=notification_text,
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Error sending notification to user {user_id}: {e}")
    
    def load_monitored_addresses(self):
        """Load monitored addresses from database"""
        conn = sqlite3.connect('pharos_bot.db')
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, wallet_address FROM users WHERE wallet_address IS NOT NULL')
        results = cursor.fetchall()
        conn.close()
        
        for user_id, wallet_address in results:
            self.pharos_monitor.monitored_addresses[wallet_address] = user_id
    
    async def monitor_transactions(self):
        """Main monitoring loop"""
        logger.info("🔄 Starting transaction monitoring...")
        
        while True:
            try:
                if not self.pharos_monitor.monitored_addresses:
                    await asyncio.sleep(10)  # Wait if no addresses to monitor
                    continue
                
                latest_block = await self.pharos_monitor.get_latest_block()
                
                if latest_block > self.pharos_monitor.last_checked_block:
                    logger.info(f"Checking blocks {self.pharos_monitor.last_checked_block + 1} to {latest_block}")
                    
                    # Check new blocks (limit to 10 blocks at once to avoid overload)
                    start_block = self.pharos_monitor.last_checked_block + 1
                    end_block = min(latest_block + 1, start_block + 10)
                    
                    for block_num in range(start_block, end_block):
                        transactions = await self.pharos_monitor.check_transactions_in_block(block_num)
                        
                        for tx in transactions:
                            await self.send_transaction_notification(tx['user_id'], tx)
                            
                            # Store transaction in database
                            try:
                                conn = sqlite3.connect('pharos_bot.db')
                                cursor = conn.cursor()
                                cursor.execute(
                                    'INSERT OR IGNORE INTO tracked_transactions (tx_hash, user_id, block_number) VALUES (?, ?, ?)',
                                    (tx['tx_hash'], tx['user_id'], tx['block_number'])
                                )
                                conn.commit()
                                conn.close()
                            except Exception as db_error:
                                logger.error(f"Database error: {db_error}")
                    
                    self.pharos_monitor.last_checked_block = end_block - 1
                
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(10)  # Wait longer on error
    
    async def run(self):
        """Run the bot"""
        # Check if bot token is provided
        if not BOT_TOKEN:
            logger.error("❌ BOT_TOKEN not found! Please add it to Secrets.")
            logger.error("Go to Tools > Secrets and add BOT_TOKEN with your bot token value")
            return
        
        # Test Web3 connection
        try:
            if self.pharos_monitor.w3.is_connected():
                logger.info("✅ Successfully connected to Pharos Testnet")
                latest_block = await self.pharos_monitor.get_latest_block()
                logger.info(f"Current block: {latest_block}")
                self.pharos_monitor.last_checked_block = max(0, latest_block - 1)  # Start from previous block
            else:
                logger.error("❌ Failed to connect to Pharos Testnet")
                return
        except Exception as e:
            logger.error(f"Error testing Web3 connection: {e}")
            return
        
        # Initialize database
        init_database()
        
        # Load existing monitored addresses
        self.load_monitored_addresses()
        
        # Create application
        self.application = Application.builder().token(BOT_TOKEN).build()
        
        # Add handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("register", self.register_command))
        self.application.add_handler(CommandHandler("forceregister", self.force_register_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("unregister", self.unregister_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        
        # Start bot
        await self.application.initialize()
        await self.application.start()
        
        # Start monitoring in background
        asyncio.create_task(self.monitor_transactions())
        
        # Start polling
        await self.application.updater.start_polling()
        
        logger.info("🤖 Bot started successfully!")
        logger.info(f"📡 Monitoring {len(self.pharos_monitor.monitored_addresses)} addresses")
        logger.info(f"🔗 Connected to Pharos Testnet (Chain ID: {CHAIN_ID})")
        
        # Keep running
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        finally:
            await self.application.stop()

if __name__ == "__main__":
    bot = TelegramBot()
    asyncio.run(bot.run())

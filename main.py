# main.py

import os
import sys
import sqlite3
import logging
import html
import fcntl
from datetime import datetime, timedelta
import re
import asyncio
from telegram import Update, ChatMember
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# ------------------- Configuration -------------------

# مسار قاعدة بيانات SQLite
DATABASE = 'warnings.db'

# معرف المستخدم المسموح له بتنفيذ الأوامر (استبدله بمعرفك الخاص)
ALLOWED_USER_ID = 6177929931  # مثال: 6177929931

# مسار ملف القفل
LOCK_FILE = '/tmp/telegram_bot.lock'  # يمكن تغيير المسار حسب الحاجة

# الإطار الزمني (بالثواني) لحذف الرسائل بعد إزالة المستخدم
MESSAGE_DELETE_TIMEFRAME = 15  # تم زيادة الوقت إلى 15 ثانية لالتقاط رسائل النظام بشكل أفضل

# ------------------- Logging Configuration -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # يمكن تغييره إلى DEBUG لمزيد من التفاصيل
)
logger = logging.getLogger(__name__)

# ------------------- Pending Group Names & Removals -------------------

# قاموس لتتبع أسماء المجموعات المعلقة
pending_group_names = {}

# قاموس لتتبع عمليات إزالة المستخدمين المعلقة
# الصيغة: {user_id: group_id}
pending_user_removals = {}

# ------------------- Lock Mechanism -------------------

def acquire_lock():
    """
    الحصول على قفل لضمان تشغيل نسخة واحدة فقط من البوت.
    """
    try:
        lock = open(LOCK_FILE, 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("تم الحصول على القفل. بدء البوت...")
        return lock
    except IOError:
        logger.error("هناك نسخة أخرى من البوت تعمل بالفعل. الإنهاء.")
        sys.exit("هناك نسخة أخرى من البوت تعمل بالفعل.")

def release_lock(lock):
    """
    تحرير القفل الذي تم الحصول عليه.
    """
    try:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        os.remove(LOCK_FILE)
        logger.info("تم تحرير القفل. توقف البوت.")
    except Exception as e:
        logger.error(f"خطأ في تحرير القفل: {e}")

# الحصول على القفل في البداية
lock = acquire_lock()

# التأكد من تحرير القفل عند الخروج
import atexit
atexit.register(release_lock, lock)

# ------------------- Database Initialization -------------------

def init_permissions_db():
    """
    تهيئة جداول الأذونات والمستخدمين المحذوفين.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        # إنشاء جدول الأذونات
        c.execute('''
            CREATE TABLE IF NOT EXISTS permissions (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL
            )
        ''')
        
        # إنشاء جدول المستخدمين المحذوفين مع group_id
        c.execute('''
            CREATE TABLE IF NOT EXISTS removed_users (
                group_id INTEGER,
                user_id INTEGER,
                removal_reason TEXT,
                removal_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, user_id),
                FOREIGN KEY (group_id) REFERENCES groups(group_id)
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("تم تهيئة جداول الأذونات والمستخدمين المحذوفين بنجاح.")
    except Exception as e:
        logger.error(f"فشل في تهيئة قاعدة بيانات الأذونات: {e}")
        raise

def init_db():
    """
    تهيئة قاعدة بيانات SQLite وإنشاء الجداول اللازمة إذا لم تكن موجودة.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute("PRAGMA foreign_keys = 1")  # تفعيل قيود المفاتيح الأجنبية
        c = conn.cursor()

        # إنشاء جدول المجموعات
        c.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                group_id INTEGER PRIMARY KEY,
                group_name TEXT
            )
        ''')

        # إنشاء جدول المستخدمين الذين يتم تجاوزهم
        c.execute('''
            CREATE TABLE IF NOT EXISTS bypass_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')

        # إنشاء جدول إعدادات الحذف
        c.execute('''
            CREATE TABLE IF NOT EXISTS deletion_settings (
                group_id INTEGER PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT 0,
                FOREIGN KEY(group_id) REFERENCES groups(group_id)
            )
        ''')

        # إنشاء جدول المستخدمين
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT
            )
        ''')

        conn.commit()
        conn.close()
        logger.info("تم تهيئة قاعدة البيانات بنجاح.")
        
        # تهيئة جداول الأذونات والمستخدمين المحذوفين
        init_permissions_db()
    except Exception as e:
        logger.error(f"فشل في تهيئة قاعدة البيانات: {e}")
        raise

# ------------------- Database Helper Functions -------------------

def add_group(group_id):
    """
    إضافة مجموعة باستخدام معرف الدردشة الخاص بها.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO groups (group_id, group_name) VALUES (?, ?)', (group_id, None))
        conn.commit()
        conn.close()
        logger.info(f"تمت إضافة المجموعة {group_id} إلى قاعدة البيانات بدون اسم.")
    except Exception as e:
        logger.error(f"خطأ في إضافة المجموعة {group_id}: {e}")
        raise

def set_group_name(g_id, group_name):
    """
    تعيين اسم للمجموعة.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE groups SET group_name = ? WHERE group_id = ?', (group_name, g_id))
        conn.commit()
        conn.close()
        logger.info(f"تم تعيين اسم للمجموعة {g_id}: {group_name}")
    except Exception as e:
        logger.error(f"خطأ في تعيين اسم المجموعة {g_id}: {e}")
        raise

def group_exists(group_id):
    """
    التحقق مما إذا كانت المجموعة موجودة في قاعدة البيانات.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM groups WHERE group_id = ?', (group_id,))
        exists = c.fetchone() is not None
        conn.close()
        logger.debug(f"التحقق من وجود المجموعة {group_id}: {exists}")
        return exists
    except Exception as e:
        logger.error(f"خطأ في التحقق من وجود المجموعة {group_id}: {e}")
        return False

def is_bypass_user(user_id):
    """
    التحقق مما إذا كان المستخدم في قائمة التجاوز.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM bypass_users WHERE user_id = ?', (user_id,))
        res = c.fetchone() is not None
        conn.close()
        logger.debug(f"التحقق مما إذا كان المستخدم {user_id} يتجاوز: {res}")
        return res
    except Exception as e:
        logger.error(f"خطأ في التحقق من حالة التجاوز للمستخدم {user_id}: {e}")
        return False

def add_bypass_user(user_id):
    """
    إضافة مستخدم إلى قائمة التجاوز.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO bypass_users (user_id) VALUES (?)', (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"تمت إضافة المستخدم {user_id} إلى قائمة التجاوز.")
    except Exception as e:
        logger.error(f"خطأ في إضافة المستخدم {user_id} إلى قائمة التجاوز: {e}")
        raise

def remove_bypass_user(user_id):
    """
    إزالة مستخدم من قائمة التجاوز.
    يُرجع True إذا تم الحذف، False إذا لم يكن موجودًا.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM bypass_users WHERE user_id = ?', (user_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            logger.info(f"تمت إزالة المستخدم {user_id} من قائمة التجاوز.")
            return True
        else:
            logger.warning(f"المستخدم {user_id} غير موجود في قائمة التجاوز.")
            return False
    except Exception as e:
        logger.error(f"خطأ في إزالة المستخدم {user_id} من قائمة التجاوز: {e}")
        return False

def enable_deletion(group_id):
    """
    تفعيل حذف الرسائل لمجموعة معينة.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            INSERT INTO deletion_settings (group_id, enabled)
            VALUES (?, 1)
            ON CONFLICT(group_id) DO UPDATE SET enabled=1
        ''', (group_id,))
        conn.commit()
        conn.close()
        logger.info(f"تم تفعيل حذف الرسائل للمجموعة {group_id}.")
    except Exception as e:
        logger.error(f"خطأ في تفعيل الحذف للمجموعة {group_id}: {e}")
        raise

def disable_deletion(group_id):
    """
    تعطيل حذف الرسائل لمجموعة معينة.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            INSERT INTO deletion_settings (group_id, enabled)
            VALUES (?, 0)
            ON CONFLICT(group_id) DO UPDATE SET enabled=0
        ''', (group_id,))
        conn.commit()
        conn.close()
        logger.info(f"تم تعطيل حذف الرسائل للمجموعة {group_id}.")
    except Exception as e:
        logger.error(f"خطأ في تعطيل الحذف للمجموعة {group_id}: {e}")
        raise

def is_deletion_enabled(group_id):
    """
    التحقق مما إذا كان حذف الرسائل مفعلاً لمجموعة معينة.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT enabled FROM deletion_settings WHERE group_id = ?', (group_id,))
        row = c.fetchone()
        conn.close()
        enabled = row[0] if row else False
        logger.debug(f"حذف الرسائل مفعّل للمجموعة {group_id}: {enabled}")
        return bool(enabled)
    except Exception as e:
        logger.error(f"خطأ في التحقق من حالة الحذف للمجموعة {group_id}: {e}")
        return False

def remove_user_from_removed_users(group_id, user_id):
    """
    إزالة مستخدم من جدول removed_users لمجموعة معينة.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, user_id))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            logger.info(f"تمت إزالة المستخدم {user_id} من قائمة المستخدمين المحذوفين للمجموعة {group_id}.")
            return True
        else:
            logger.warning(f"المستخدم {user_id} غير موجود في قائمة المستخدمين المحذوفين للمجموعة {group_id}.")
            return False
    except Exception as e:
        logger.error(f"خطأ في إزالة المستخدم {user_id} من قائمة المحذوفين للمجموعة {group_id}: {e}")
        return False

def revoke_user_permissions(user_id):
    """
    إلغاء جميع أذونات المستخدم عن طريق تعيين دوره إلى 'removed'.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE permissions SET role = ? WHERE user_id = ?', ('removed', user_id))
        conn.commit()
        conn.close()
        logger.info(f"تم إلغاء أذونات المستخدم {user_id}. تم تعيين دوره إلى 'removed'.")
    except Exception as e:
        logger.error(f"خطأ في إلغاء أذونات المستخدم {user_id}: {e}")
        raise

def list_removed_users(group_id=None):
    """
    استرجاع جميع المستخدمين من جدول removed_users.
    إذا تم توفير group_id، يتم التصفية بناءً على تلك المجموعة.
    يُرجع قائمة من tuples تحتوي على user_id، removal_reason، و removal_time.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        if group_id:
            c.execute('SELECT user_id, removal_reason, removal_time FROM removed_users WHERE group_id = ?', (group_id,))
        else:
            c.execute('SELECT group_id, user_id, removal_reason, removal_time FROM removed_users')
        users = c.fetchall()
        conn.close()
        logger.info("تم استرجاع قائمة المستخدمين المحذوفين.")
        return users
    except Exception as e:
        logger.error(f"خطأ في استرجاع المستخدمين المحذوفين: {e}")
        return []

# ------------------- Flag for Message Deletion -------------------

# قاموس لتتبع المجموعات التي يجب حذف رسائلها بعد الإزالة
# الصيغة: {group_id: expiration_time}
delete_all_messages_after_removal = {}

# ------------------- Command Handler Functions -------------------

async def handle_private_message_for_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الرسائل الخاصة التي يرسلها المستخدم المصرح له لتعيين اسم المجموعة.
    """
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    message = update.message
    user = message.from_user
    logger.debug(f"تم استقبال رسالة خاصة من المستخدم {user.id}: {message.text}")
    if user.id == ALLOWED_USER_ID and user.id in pending_group_names:
        g_id = pending_group_names.pop(user.id)
        group_name = message.text.strip()
        if group_name:
            try:
                escaped_group_name = escape_markdown(group_name, version=2)
                set_group_name(g_id, group_name)
                confirmation_message = escape_markdown(
                    f"✅ تم تعيين اسم المجموعة `{g_id}` إلى: *{escaped_group_name}*",
                    version=2
                )
                await context.bot.send_message(
                    chat_id=user.id,
                    text=confirmation_message,
                    parse_mode='MarkdownV2'
                )
                logger.info(f"تم تعيين اسم المجموعة {g_id} إلى {group_name} بواسطة المستخدم {user.id}")
            except Exception as e:
                error_message = escape_markdown("⚠️ فشل في تعيين اسم المجموعة. الرجاء محاولة `/group_add` مرة أخرى.", version=2)
                await context.bot.send_message(
                    chat_id=user.id,
                    text=error_message,
                    parse_mode='MarkdownV2'
                )
                logger.error(f"خطأ في تعيين اسم المجموعة {g_id} بواسطة المستخدم {user.id}: {e}")
        else:
            warning_message = escape_markdown("⚠️ لا يمكن أن يكون اسم المجموعة فارغًا. الرجاء محاولة `/group_add` مرة أخرى.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"تم استقبال اسم مجموعة فارغ من المستخدم {user.id} للمجموعة {g_id}")
    else:
        warning_message = escape_markdown("⚠️ لا توجد مجموعة معلقة لتعيين اسم لها.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=warning_message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم استقبال اسم مجموعة من المستخدم {user.id} بدون مجموعة معلقة.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /start.
    """
    try:
        user = update.effective_user
        if user.id != ALLOWED_USER_ID:
            return  # تجاهل المستخدمين غير المصرح لهم
        message = escape_markdown("✅ البوت يعمل وجاهز.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"تم استدعاء /start بواسطة المستخدم {user.id}")
    except Exception as e:
        logger.error(f"خطأ في التعامل مع أمر /start: {e}")

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /group_add لتسجيل مجموعة.
    الاستخدام: /group_add <group_id>
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /group_add بواسطة المستخدم {user.id} مع الوسائط: {context.args}")
    
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له

    if len(context.args) != 1:
        message = escape_markdown("⚠️ الاستخدام: `/group_add <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /group_add بواسطة المستخدم {user.id}")
        return

    try:
        group_id = int(context.args[0])
        logger.debug(f"تم تحليل group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` يجب أن يكون عددًا صحيحًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم group_id غير صحيح إلى /group_add بواسطة المستخدم {user.id}")
        return

    if group_exists(group_id):
        message = escape_markdown("⚠️ المجموعة مضافة بالفعل.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.debug(f"المجموعة {group_id} موجودة بالفعل.")
        return

    try:
        add_group(group_id)
        logger.debug(f"تمت إضافة المجموعة {group_id} إلى قاعدة البيانات.")
    except Exception as e:
        message = escape_markdown("⚠️ فشل في إضافة المجموعة. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"فشل في إضافة المجموعة {group_id} بواسطة المستخدم {user.id}: {e}")
        return

    pending_group_names[user.id] = group_id
    logger.info(f"تمت إضافة المجموعة {group_id}، ينتظر اسم المجموعة من المستخدم {user.id} في الدردشة الخاصة.")
    
    try:
        confirmation_message = escape_markdown(
            f"✅ تمت إضافة المجموعة `{group_id}`.\nيرجى إرسال اسم المجموعة في رسالة خاصة للبوت.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logger.error(f"خطأ في إرسال التأكيد لأمر /group_add: {e}")

async def rmove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /rmove_group لإزالة مجموعة مسجلة.
    الاستخدام: /rmove_group <group_id>
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /rmove_group بواسطة المستخدم {user.id} مع الوسائط: {context.args}")
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له
    if len(context.args) != 1:
        message = escape_markdown("⚠️ الاستخدام: `/rmove_group <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /rmove_group بواسطة المستخدم {user.id}")
        return
    try:
        group_id = int(context.args[0])
        logger.debug(f"تم تحليل group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` يجب أن يكون عددًا صحيحًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم group_id غير صحيح إلى /rmove_group بواسطة المستخدم {user.id}")
        return

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM groups WHERE group_id = ?', (group_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            confirm_message = escape_markdown(
                f"✅ تمت إزالة المجموعة `{group_id}` من التسجيل.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=confirm_message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"تمت إزالة المجموعة {group_id} بواسطة المستخدم {user.id}")
        else:
            warning_message = escape_markdown(
                f"⚠️ المجموعة `{group_id}` غير موجودة.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"محاولة إزالة مجموعة غير موجودة {group_id} بواسطة المستخدم {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ فشل في إزالة المجموعة. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في إزالة المجموعة {group_id} بواسطة المستخدم {user.id}: {e}")

async def bypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /bypass لإضافة مستخدم إلى قائمة التجاوز.
    الاستخدام: /bypass <user_id>
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /bypass بواسطة المستخدم {user.id} مع الوسائط: {context.args}")
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له

    if len(context.args) != 1:
        message = escape_markdown("⚠️ الاستخدام: `/bypass <user_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /bypass بواسطة المستخدم {user.id}")
        return

    try:
        target_user_id = int(context.args[0])
        logger.debug(f"تم تحليل target_user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ `user_id` يجب أن يكون عددًا صحيحًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم user_id غير صحيح إلى /bypass بواسطة المستخدم {user.id}")
        return

    # التحقق مما إذا كان المستخدم بالفعل في قائمة التجاوز
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM bypass_users WHERE user_id = ?', (target_user_id,))
        if c.fetchone():
            conn.close()
            message = escape_markdown(f"⚠️ المستخدم `{target_user_id}` موجود بالفعل في قائمة التجاوز.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"محاولة إضافة مستخدم متجاوز بالفعل {target_user_id} بواسطة المستخدم {user.id}")
            return
        conn.close()
    except Exception as e:
        message = escape_markdown("⚠️ فشل في التحقق من حالة التجاوز. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في التحقق من حالة التجاوز للمستخدم {target_user_id}: {e}")
        return

    try:
        add_bypass_user(target_user_id)
        confirmation_message = escape_markdown(
            f"✅ تم إضافة المستخدم `{target_user_id}` إلى تجاوز التحذيرات.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"تمت إضافة المستخدم {target_user_id} إلى قائمة التجاوز بواسطة المستخدم {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ فشل في إضافة المستخدم إلى قائمة التجاوز. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في إضافة المستخدم {target_user_id} إلى قائمة التجاوز بواسطة المستخدم {user.id}: {e}")

async def unbypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /unbypass لإزالة مستخدم من قائمة التجاوز.
    الاستخدام: /unbypass <user_id>
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /unbypass بواسطة المستخدم {user.id} مع الوسائط: {context.args}")
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له
    if len(context.args) != 1:
        message = escape_markdown("⚠️ الاستخدام: `/unbypass <user_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /unbypass بواسطة المستخدم {user.id}")
        return
    try:
        target_user_id = int(context.args[0])
        logger.debug(f"تم تحليل target_user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ `user_id` يجب أن يكون عددًا صحيحًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم user_id غير صحيح إلى /unbypass بواسطة المستخدم {user.id}")
        return

    try:
        if remove_bypass_user(target_user_id):
            confirmation_message = escape_markdown(
                f"✅ تم إزالة المستخدم `{target_user_id}` من تجاوز التحذيرات.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=confirmation_message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"تمت إزالة المستخدم {target_user_id} من قائمة التجاوز بواسطة المستخدم {user.id}")
        else:
            warning_message = escape_markdown(
                f"⚠️ المستخدم `{target_user_id}` لم يكن في قائمة التجاوز.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"محاولة إزالة مستخدم غير موجود في قائمة التجاوز {target_user_id} بواسطة المستخدم {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ فشل في إزالة المستخدم من قائمة التجاوز. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في إزالة المستخدم {target_user_id} من قائمة التجاوز بواسطة المستخدم {user.id}: {e}")

async def show_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /show أو /list لعرض جميع المجموعات وإعداداتها.
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /show بواسطة المستخدم {user.id}")
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT group_id, group_name FROM groups')
        groups_data = c.fetchall()
        conn.close()

        if not groups_data:
            message = escape_markdown("⚠️ لم تتم إضافة أي مجموعات.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.debug("لم يتم العثور على مجموعات في قاعدة البيانات.")
            return

        msg = "*معلومات المجموعات:*\n\n"
        for g_id, g_name in groups_data:
            g_name_display = g_name if g_name else "لم يتم تعيين اسم"
            g_name_esc = escape_markdown(g_name_display, version=2)
            msg += f"*المجموعة:* {g_name_esc}\n*معرف المجموعة:* `{g_id}`\n"

            # جلب إعدادات الحذف
            try:
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute('SELECT enabled FROM deletion_settings WHERE group_id = ?', (g_id,))
                row = c.fetchone()
                conn.close()
                deletion_status = "مفعل" if row and row[0] else "معطل"
                msg += f"*حالة الحذف:* `{deletion_status}`\n"
            except Exception as e:
                msg += "⚠️ خطأ في جلب حالة الحذف.\n"
                logger.error(f"خطأ في جلب حالة الحذف للمجموعة {g_id}: {e}")

            msg += "\n"

        try:
            # حد طول الرسالة في Telegram هو 4096 حرفًا
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    chunk = msg[i:i+4000]
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=chunk,
                        parse_mode='MarkdownV2'
                    )
            else:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=msg,
                    parse_mode='MarkdownV2'
                )
            logger.info("تم عرض نظرة شاملة على البوت.")
        except Exception as e:
            logger.error(f"خطأ في إرسال معلومات /show: {e}")
            message = escape_markdown("⚠️ حدث خطأ أثناء إرسال معلومات القائمة.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"خطأ في معالجة أمر /show: {e}")
        message = escape_markdown("⚠️ فشل في استرجاع معلومات القائمة. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

async def group_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /group_id لاسترجاع معرف المجموعة أو المستخدم.
    """
    user = update.effective_user
    group = update.effective_chat
    user_id = user.id
    logger.debug(f"تم استدعاء أمر /group_id بواسطة المستخدم {user_id} في الدردشة {group.id}")
    
    if user_id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له
    
    try:
        if group.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            group_id = group.id
            message = escape_markdown(f"🔢 *معرف المجموعة:* `{group_id}`", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"تم إرسال معرف المجموعة {group_id} إلى المستخدم {user_id}")
        else:
            # إذا كانت الدردشة خاصة
            message = escape_markdown(f"🔢 *معرفك الشخصي:* `{user_id}`", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"تم إرسال معرف المستخدم {user_id} في الدردشة الخاصة.")
    except Exception as e:
        logger.error(f"خطأ في التعامل مع أمر /group_id: {e}")
        message = escape_markdown("⚠️ حدث خطأ أثناء معالجة الأمر.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /help لعرض الأوامر المتاحة.
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /help بواسطة المستخدم {user.id}, ALLOWED_USER_ID={ALLOWED_USER_ID}")
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له
    help_text = """*الأوامر المتاحة:*
• `/start` - التحقق مما إذا كان البوت يعمل
• `/group_add <group_id>` - تسجيل مجموعة (استخدم معرف الدردشة الفعلي للمجموعة)
• `/rmove_group <group_id>` - إزالة مجموعة مسجلة
• `/bypass <user_id>` - إضافة مستخدم إلى تجاوز التحذيرات
• `/unbypass <user_id>` - إزالة مستخدم من تجاوز التحذيرات
• `/group_id` - استرجاع معرف المجموعة الحالي أو معرف المستخدم
• `/show` - عرض جميع المجموعات وحالة الحذف
• `/info` - عرض تكوين البوت الحالي
• `/help` - عرض هذه المساعدة
• `/list` - نظرة شاملة على المجموعات والمستخدمين المتجاوزين
• `/be_sad <group_id>` - تفعيل حذف الرسائل العربية في المجموعة
• `/be_happy <group_id>` - تعطيل حذف الرسائل العربية في المجموعة
• `/rmove_user <group_id> <user_id>` - إزالة مستخدم من مجموعة بدون إرسال إشعارات
• `/add_removed_user <group_id> <user_id>` - إضافة مستخدم إلى قائمة "المستخدمين المحذوفين" لمجموعة محددة
• `/list_removed_users` - عرض جميع المستخدمين في قائمة "المستخدمين المحذوفين" لكل مجموعة
• `/list_rmoved_rmove <group_id>` - طلب إزالة مستخدم من قائمة "المستخدمين المحذوفين" لمجموعة محددة
• `/check <group_id>` - التحقق من قائمة "المستخدمين المحذوفين" مقابل الأعضاء الفعليين في المجموعة وإزالة أي تناقضات
"""
    try:
        # الهروب من الأحرف الخاصة لـ MarkdownV2
        help_text_esc = escape_markdown(help_text, version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=help_text_esc,
            parse_mode='MarkdownV2'
        )
        logger.info("تم عرض معلومات المساعدة للمستخدم.")
    except Exception as e:
        logger.error(f"خطأ في إرسال معلومات المساعدة: {e}")
        message = escape_markdown("⚠️ حدث خطأ أثناء إرسال معلومات المساعدة.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /info لعرض التكوين الحالي.
    """
    user = update.effective_user
    user_id = user.id
    logger.debug(f"تم استدعاء أمر /info بواسطة المستخدم {user_id}")

    if user_id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()

        # جلب جميع المجموعات وإعدادات الحذف الخاصة بها
        c.execute('''
            SELECT g.group_id, g.group_name, ds.enabled
            FROM groups g
            LEFT JOIN deletion_settings ds ON g.group_id = ds.group_id
        ''')
        groups = c.fetchall()

        # جلب جميع المستخدمين المتجاوزين
        c.execute('''
            SELECT user_id FROM bypass_users
        ''')
        bypass_users = c.fetchall()

        conn.close()

        msg = "*معلومات البوت:*\n\n"
        msg += "*المجموعات المسجلة:*\n"
        if groups:
            for g_id, g_name, enabled in groups:
                g_name_display = g_name if g_name else "لم يتم تعيين اسم"
                deletion_status = "مفعل" if enabled else "معطل"
                msg += f"• *اسم المجموعة:* {escape_markdown(g_name_display, version=2)}\n"
                msg += f"  *معرف المجموعة:* `{g_id}`\n"
                msg += f"  *الحذف:* `{deletion_status}`\n\n"
        else:
            msg += "⚠️ لم تتم إضافة أي مجموعات.\n\n"

        msg += "*المستخدمين المتجاوزين:*\n"
        if bypass_users:
            for (b_id,) in bypass_users:
                msg += f"• *معرف المستخدم:* `{b_id}`\n"
        else:
            msg += "⚠️ لم يتم تجاوز أي مستخدمين.\n"

        try:
            # حد طول الرسالة في Telegram هو 4096 حرفًا
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    chunk = msg[i:i+4000]
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=chunk,
                        parse_mode='MarkdownV2'
                    )
            else:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=msg,
                    parse_mode='MarkdownV2'
                )
            logger.info("تم عرض معلومات البوت.")
        except Exception as e:
            logger.error(f"خطأ في إرسال معلومات /info: {e}")
            message = escape_markdown("⚠️ حدث خطأ أثناء إرسال المعلومات.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"خطأ في معالجة أمر /info: {e}")
        message = escape_markdown("⚠️ فشل في استرجاع المعلومات. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

# ------------------- New Commands: /add_removed_user & /list_removed_users -------------------

async def add_removed_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /add_removed_user لإضافة مستخدم إلى قائمة "المستخدمين المحذوفين" لمجموعة معينة.
    الاستخدام: /add_removed_user <group_id> <user_id>
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /add_removed_user بواسطة المستخدم {user.id} مع الوسائط: {context.args}")
    
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له

    if len(context.args) != 2:
        message = escape_markdown("⚠️ الاستخدام: `/add_removed_user <group_id> <user_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /add_removed_user بواسطة المستخدم {user.id}")
        return

    try:
        group_id = int(context.args[0])
        target_user_id = int(context.args[1])
        logger.debug(f"تم تحليل group_id: {group_id}, user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ كلا من `group_id` و `user_id` يجب أن يكونا أعدادًا صحيحة.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم group_id أو user_id غير صحيحين إلى /add_removed_user بواسطة المستخدم {user.id}")
        return

    if not group_exists(group_id):
        message = escape_markdown(f"⚠️ المجموعة `{group_id}` غير مسجلة. الرجاء إضافتها باستخدام `/group_add {group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"محاولة إضافة مستخدم محذوف إلى مجموعة غير مسجلة {group_id} بواسطة المستخدم {user.id}")
        return

    # التحقق مما إذا كان المستخدم موجودًا بالفعل في قائمة "المستخدمين المحذوفين" للمجموعة
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, target_user_id))
        if c.fetchone():
            conn.close()
            message = escape_markdown(f"⚠️ المستخدم `{target_user_id}` موجود بالفعل في قائمة 'المستخدمين المحذوفين' للمجموعة `{group_id}`.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"محاولة إضافة مستخدم محذوف موجود بالفعل {target_user_id} إلى المجموعة {group_id} بواسطة المستخدم {user.id}")
            return
        conn.close()
    except Exception as e:
        message = escape_markdown("⚠️ فشل في التحقق من قائمة المستخدمين المحذوفين. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في التحقق من قائمة المستخدمين المحذوفين للمجموعة {group_id}: {e}")
        return

    try:
        # إضافة المستخدم إلى قائمة "المستخدمين المحذوفين"
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            INSERT INTO removed_users (group_id, user_id, removal_reason)
            VALUES (?, ?, ?)
        ''', (group_id, target_user_id, "تمت الإضافة يدويًا عبر /add_removed_user"))
        conn.commit()
        conn.close()
        confirmation_message = escape_markdown(
            f"✅ تمت إضافة المستخدم `{target_user_id}` إلى قائمة 'المستخدمين المحذوفين' للمجموعة `{group_id}`.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"تمت إضافة المستخدم {target_user_id} إلى قائمة 'المستخدمين المحذوفين' للمجموعة {group_id} بواسطة المستخدم {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ فشل في إضافة المستخدم إلى قائمة 'المستخدمين المحذوفين'. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في إضافة المستخدم {target_user_id} إلى قائمة 'المستخدمين المحذوفين' للمجموعة {group_id} بواسطة المستخدم {user.id}: {e}")

async def list_removed_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /list_removed_users لعرض جميع المستخدمين في قائمة "المستخدمين المحذوفين" لكل مجموعة.
    الاستخدام: /list_removed_users
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /list_removed_users بواسطة المستخدم {user.id}")
    
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له

    try:
        removed_users = list_removed_users()
        if not removed_users:
            message = escape_markdown("⚠️ قائمة 'المستخدمين المحذوفين' فارغة.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.info("تم عرض قائمة 'المستخدمين المحذوفين' الفارغة.")
            return

        # تنظيم المستخدمين المحذوفين حسب المجموعة
        groups = {}
        for group_id, user_id, reason, time in removed_users:
            if group_id not in groups:
                groups[group_id] = []
            groups[group_id].append((user_id, reason, time))

        msg = "*المستخدمين المحذوفين:*\n\n"
        for group_id, users in groups.items():
            msg += f"*معرف المجموعة:* `{group_id}`\n"
            for user_id, reason, time in users:
                msg += f"• *معرف المستخدم:* `{user_id}`\n"
                msg += f"  *السبب:* {escape_markdown(reason, version=2)}\n"
                msg += f"  *وقت الإزالة:* {time}\n"
            msg += "\n"

        try:
            # حد طول الرسالة في Telegram هو 4096 حرفًا
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    chunk = msg[i:i+4000]
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=chunk,
                        parse_mode='MarkdownV2'
                    )
            else:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=msg,
                    parse_mode='MarkdownV2'
                )
            logger.info("تم عرض قائمة 'المستخدمين المحذوفين'.")
        except Exception as e:
            logger.error(f"خطأ في إرسال قائمة 'المستخدمين المحذوفين': {e}")
            message = escape_markdown("⚠️ حدث خطأ أثناء إرسال القائمة.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"خطأ في معالجة أمر /list_removed_users: {e}")
        message = escape_markdown("⚠️ فشل في استرجاع قائمة 'المستخدمين المحذوفين'. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

# ------------------- New /list_rmoved_rmove Command -------------------

async def list_rmoved_rmove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /list_rmoved_rmove لطلب إزالة مستخدم من قائمة "المستخدمين المحذوفين" لمجموعة محددة.
    الاستخدام: /list_rmoved_rmove <group_id>
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /list_rmoved_rmove بواسطة المستخدم {user.id} مع الوسائط: {context.args}")
    
    if user.id != ALLOWED_USER_ID:
        logger.warning(f"محاولة وصول غير مصرح بها من قبل المستخدم {user.id} لاستخدام /list_rmoved_rmove.")
        return  # الرد فقط للمستخدم المصرح له

    if len(context.args) != 1:
        message = escape_markdown("⚠️ الاستخدام: `/list_rmoved_rmove <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /list_rmoved_rmove بواسطة المستخدم {user.id}")
        return

    try:
        group_id = int(context.args[0])
        logger.debug(f"تم تحليل group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` يجب أن يكون عددًا صحيحًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم group_id غير صحيح إلى /list_rmoved_rmove بواسطة المستخدم {user.id}")
        return

    if not group_exists(group_id):
        message = escape_markdown(f"⚠️ المجموعة `{group_id}` غير مسجلة. الرجاء إضافتها باستخدام `/group_add {group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"محاولة استخدام /list_rmoved_rmove لمجموعة غير مسجلة {group_id} بواسطة المستخدم {user.id}")
        return

    # تعيين عملية إزالة معلقة للمستخدم
    pending_user_removals[user.id] = group_id
    logger.info(f"تم تعيين إزالة مستخدم من مجموعة {group_id} بواسطة المستخدم {user.id}")

    # طلب من المستخدم إدخال معرف المستخدم لإزالته من القائمة
    try:
        prompt_message = escape_markdown(
            f"يرجى إرسال `user_id` للمستخدم الذي ترغب في إزالته من قائمة 'المستخدمين المحذوفين' للمجموعة `{group_id}`.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=prompt_message,
            parse_mode='MarkdownV2'
        )
        logger.debug(f"تم إرسال طلب إزالة مستخدم من مجموعة {group_id} إلى المستخدم {user.id}")
    except Exception as e:
        logger.error(f"خطأ في إرسال طلب إزالة المستخدم من مجموعة {group_id}: {e}")

# ------------------- Existing /rmove_user Command -------------------

async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /rmove_user لإزالة مستخدم من مجموعة بدون إرسال إشعارات.
    الاستخدام: /rmove_user <group_id> <user_id>
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /rmove_user بواسطة المستخدم {user.id} مع الوسائط: {context.args}")

    # التحقق مما إذا كان المستخدم مصرحًا له
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له

    # التحقق من عدد الوسائط الصحيحة
    if len(context.args) != 2:
        message = escape_markdown("⚠️ الاستخدام: `/rmove_user <group_id> <user_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /rmove_user بواسطة المستخدم {user.id}")
        return

    # تحليل group_id و user_id
    try:
        group_id = int(context.args[0])
        target_user_id = int(context.args[1])
        logger.debug(f"تم تحليل group_id: {group_id}, user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ كلا من `group_id` و `user_id` يجب أن يكونا أعدادًا صحيحة.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم group_id أو user_id غير صحيحين إلى /rmove_user بواسطة المستخدم {user.id}")
        return

    # إزالة المستخدم من قائمة التجاوز
    try:
        if remove_bypass_user(target_user_id):
            logger.info(f"تمت إزالة المستخدم {target_user_id} من قائمة التجاوز بواسطة المستخدم {user.id}")
        else:
            logger.info(f"المستخدم {target_user_id} لم يكن في قائمة التجاوز.")
    except Exception as e:
        message = escape_markdown("⚠️ فشل في تحديث قائمة التجاوز. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في إزالة المستخدم {target_user_id} من قائمة التجاوز: {e}")
        return

    # إزالة المستخدم من جدول removed_users
    try:
        removed = remove_user_from_removed_users(group_id, target_user_id)
        if removed:
            logger.info(f"تمت إزالة المستخدم {target_user_id} من 'المستخدمين المحذوفين' في Permissions للمجموعة {group_id} بواسطة المستخدم {user.id}")
        else:
            logger.warning(f"المستخدم {target_user_id} لم يكن في 'المستخدمين المحذوفين' للمجموعة {group_id}.")
    except Exception as e:
        message = escape_markdown("⚠️ فشل في تحديث نظام الأذونات. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في إزالة المستخدم {target_user_id} من 'المستخدمين المحذوفين' في Permissions للمجموعة {group_id}: {e}")
        return

    # إلغاء أذونات المستخدم
    try:
        revoke_user_permissions(target_user_id)
        logger.info(f"تم إلغاء أذونات المستخدم {target_user_id} في نظام الأذونات.")
    except Exception as e:
        message = escape_markdown("⚠️ فشل في إلغاء أذونات المستخدم. الرجاء التحقق من نظام الأذونات.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في إلغاء أذونات المستخدم {target_user_id}: {e}")
        return

    # محاولة إزالة المستخدم من المجموعة
    try:
        await context.bot.ban_chat_member(chat_id=group_id, user_id=target_user_id)
        logger.info(f"تمت إزالة المستخدم {target_user_id} من المجموعة {group_id} بواسطة البوت.")
    except Exception as e:
        message = escape_markdown(f"⚠️ فشل في إزالة المستخدم `{target_user_id}` من المجموعة `{group_id}`. تأكد من أن البوت لديه الأذونات اللازمة.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في إزالة المستخدم {target_user_id} من المجموعة {group_id}: {e}")
        return

    # تعيين علم لحذف أي رسائل في المجموعة خلال MESSAGE_DELETE_TIMEFRAME ثوانٍ
    delete_all_messages_after_removal[group_id] = datetime.utcnow() + timedelta(seconds=MESSAGE_DELETE_TIMEFRAME)
    logger.info(f"تم تعيين علم حذف الرسائل للمجموعة {group_id} لمدة {MESSAGE_DELETE_TIMEFRAME} ثانية.")

    # جدولة إزالة العلم بعد MESSAGE_DELETE_TIMEFRAME ثوانٍ
    asyncio.create_task(remove_deletion_flag_after_timeout(group_id))

    # إرسال تأكيد إلى المستخدم المصرح له بشكل خاص
    confirmation_message = escape_markdown(
        f"✅ تمت إزالة المستخدم `{target_user_id}` من المجموعة `{group_id}` وتمت إزالته من 'المستخدمين المحذوفين' في Permissions.\nسيتم حذف أي رسائل تُرسل إلى المجموعة خلال الـ {MESSAGE_DELETE_TIMEFRAME} ثانية القادمة.",
        version=2
    )
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"تم إرسال تأكيد إلى المستخدم {user.id} حول إزالة المستخدم {target_user_id} من المجموعة {group_id} وPermissions.")
    except Exception as e:
        logger.error(f"خطأ في إرسال رسالة التأكيد لأمر /rmove_user: {e}")

# ------------------- Message Handler Functions -------------------

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    حذف الرسائل التي تحتوي على نص عربي في المجموعات التي تم تفعيل الحذف فيها.
    """
    message = update.message
    if not message or not message.text:
        logger.debug("تم استقبال رسالة غير نصية أو فارغة.")
        return  # تجاهل الرسائل غير النصية أو الفارغة

    user = message.from_user
    chat = message.chat
    group_id = chat.id

    logger.debug(f"التحقق من الرسالة في المجموعة {group_id} من المستخدم {user.id}: {message.text}")

    # التحقق مما إذا كان الحذف مفعلًا لهذه المجموعة
    if not is_deletion_enabled(group_id):
        logger.debug(f"الحذف غير مفعل للمجموعة {group_id}.")
        return

    # التحقق مما إذا كان المستخدم يتجاوز
    if is_bypass_user(user.id):
        logger.debug(f"المستخدم {user.id} يتجاوز. لن يتم حذف الرسالة.")
        return

    # التحقق مما إذا كانت الرسالة تحتوي على نص عربي
    if is_arabic(message.text):
        try:
            await message.delete()
            logger.info(f"تم حذف رسالة عربية من المستخدم {user.id} في المجموعة {group_id}.")
            # تم إزالة رسالة التحذير لعدم إرسال إشعار للمستخدم
        except Exception as e:
            logger.error(f"خطأ في حذف الرسالة في المجموعة {group_id}: {e}")

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    حذف أي رسالة تُرسل إلى المجموعة إذا كان العلم مفعلًا.
    يشمل ذلك الرسائل من المستخدمين ورسائل النظام.
    """
    message = update.message
    if not message:
        return

    chat = message.chat
    group_id = chat.id

    # التحقق مما إذا كانت المجموعة مُعلّمة لحذف الرسائل
    if group_id in delete_all_messages_after_removal:
        try:
            await message.delete()
            logger.info(f"تم حذف رسالة في المجموعة {group_id}: {message.text or 'رسالة غير نصية.'}")
        except Exception as e:
            logger.error(f"فشل في حذف الرسالة في المجموعة {group_id}: {e}")

# ------------------- Utility Function -------------------

def is_arabic(text):
    """
    التحقق مما إذا كان النص يحتوي على أحرف عربية.
    """
    return bool(re.search(r'[\u0600-\u06FF]', text))

# ------------------- Error Handler -------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأخطاء التي تحدث أثناء التحديثات.
    """
    logger.error("حدث خطأ:", exc_info=context.error)

# ------------------- Additional Utility Function -------------------

async def remove_deletion_flag_after_timeout(group_id):
    """
    إزالة علم الحذف لمجموعة بعد انتهاء الإطار الزمني المحدد.
    """
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    delete_all_messages_after_removal.pop(group_id, None)
    logger.info(f"تمت إزالة علم حذف الرسائل للمجموعة {group_id} بعد انتهاء الإطار الزمني.")

# ------------------- Be Sad and Be Happy Commands -------------------

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /be_sad لتفعيل حذف الرسائل في مجموعة.
    الاستخدام: /be_sad <group_id>
    """
    user = update.effective_user
    args = context.args
    logger.debug(f"تم استدعاء أمر /be_sad بواسطة المستخدم {user.id} مع الوسائط: {args}")

    # التحقق مما إذا كان المستخدم مصرحًا له
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له

    if len(args) != 1:
        message = escape_markdown("⚠️ الاستخدام: `/be_sad <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /be_sad بواسطة المستخدم {user.id}")
        return

    try:
        group_id = int(args[0])
    except ValueError:
        message = escape_markdown("⚠️ `group_id` يجب أن يكون عددًا صحيحًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم group_id غير صحيح إلى /be_sad بواسطة المستخدم {user.id}")
        return

    # تفعيل الحذف
    try:
        enable_deletion(group_id)
    except Exception:
        message = escape_markdown("⚠️ فشل في تفعيل حذف الرسائل. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        return

    # تأكيد للمستخدم
    confirmation_message = escape_markdown(
        f"✅ تم تفعيل حذف الرسائل للمجموعة `{group_id}`.",
        version=2
    )
    await context.bot.send_message(
        chat_id=user.id,
        text=confirmation_message,
        parse_mode='MarkdownV2'
    )
    logger.info(f"المستخدم {user.id} قام بتفعيل حذف الرسائل للمجموعة {group_id}.")

async def be_happy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /be_happy لتعطيل حذف الرسائل في مجموعة.
    الاستخدام: /be_happy <group_id>
    """
    user = update.effective_user
    args = context.args
    logger.debug(f"تم استدعاء أمر /be_happy بواسطة المستخدم {user.id} مع الوسائط: {args}")

    # التحقق مما إذا كان المستخدم مصرحًا له
    if user.id != ALLOWED_USER_ID:
        return  # الرد فقط للمستخدم المصرح له

    if len(args) != 1:
        message = escape_markdown("⚠️ الاستخدام: `/be_happy <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /be_happy بواسطة المستخدم {user.id}")
        return

    try:
        group_id = int(args[0])
    except ValueError:
        message = escape_markdown("⚠️ `group_id` يجب أن يكون عددًا صحيحًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم group_id غير صحيح إلى /be_happy بواسطة المستخدم {user.id}")
        return

    # تعطيل الحذف
    try:
        disable_deletion(group_id)
    except Exception:
        message = escape_markdown("⚠️ فشل في تعطيل حذف الرسائل. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        return

    # تأكيد للمستخدم
    confirmation_message = escape_markdown(
        f"✅ تم تعطيل حذف الرسائل للمجموعة `{group_id}`.",
        version=2
    )
    await context.bot.send_message(
        chat_id=user.id,
        text=confirmation_message,
        parse_mode='MarkdownV2'
    )
    logger.info(f"المستخدم {user.id} قام بتعطيل حذف الرسائل للمجموعة {group_id}.")

# ------------------- Check Command -------------------

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأمر /check للتحقق من قائمة "المستخدمين المحذوفين" لمجموعة معينة.
    الاستخدام: /check <group_id>
    """
    user = update.effective_user
    logger.debug(f"تم استدعاء أمر /check بواسطة المستخدم {user.id} مع الوسائط: {context.args}")

    # التحقق من أن الأمر يُستخدم بواسطة المستخدم المصرح له
    if user.id != ALLOWED_USER_ID:
        logger.warning(f"محاولة وصول غير مصرح بها من قبل المستخدم {user.id} لاستخدام /check.")
        return  # لا ترد على المستخدمين غير المصرح لهم

    # التحقق من عدد الوسائط الصحيحة
    if len(context.args) != 1:
        message = escape_markdown("⚠️ الاستخدام: `/check <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"استخدام غير صحيح لأمر /check بواسطة المستخدم {user.id}. الوسائط المقدمة: {context.args}")
        return

    # تحليل group_id
    try:
        group_id = int(context.args[0])
        logger.debug(f"تم تحليل group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` يجب أن يكون عددًا صحيحًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم group_id غير صحيح إلى /check بواسطة المستخدم {user.id}: {context.args[0]}")
        return

    # التحقق من وجود المجموعة في قاعدة البيانات
    if not group_exists(group_id):
        message = escape_markdown(f"⚠️ المجموعة `{group_id}` غير مسجلة. الرجاء إضافتها باستخدام `/group_add {group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"محاولة استخدام /check لمجموعة غير مسجلة {group_id} بواسطة المستخدم {user.id}")
        return

    # جلب المستخدمين المحذوفين من قاعدة البيانات للمجموعة المحددة
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT user_id FROM removed_users WHERE group_id = ?', (group_id,))
        removed_users = [row[0] for row in c.fetchall()]
        conn.close()
        logger.debug(f"تم جلب المستخدمين المحذوفين للمجموعة {group_id}: {removed_users}")
    except Exception as e:
        logger.error(f"خطأ في جلب المستخدمين المحذوفين للمجموعة {group_id}: {e}")
        message = escape_markdown("⚠️ فشل في استرجاع المستخدمين المحذوفين من قاعدة البيانات.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        return

    if not removed_users:
        message = escape_markdown(f"⚠️ لم يتم العثور على مستخدمين محذوفين للمجموعة `{group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"لا يوجد مستخدمين محذوفين للتحقق في المجموعة {group_id} بواسطة المستخدم {user.id}")
        return

    # تهيئة قوائم لتتبع حالة المستخدمين
    users_still_in_group = []
    users_not_in_group = []

    # التحقق من حالة عضوية كل مستخدم في المجموعة
    for user_id in removed_users:
        try:
            member = await context.bot.get_chat_member(chat_id=group_id, user_id=user_id)
            status = member.status
            if status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                users_still_in_group.append(user_id)
                logger.debug(f"المستخدم {user_id} لا يزال عضوًا في المجموعة {group_id}. الحالة: {status}")
            else:
                users_not_in_group.append(user_id)
                logger.debug(f"المستخدم {user_id} ليس عضوًا في المجموعة {group_id}. الحالة: {status}")
        except Exception as e:
            # إذا لم يتمكن البوت من جلب حالة العضوية، نفترض أن المستخدم غير موجود في المجموعة
            users_not_in_group.append(user_id)
            logger.error(f"خطأ في جلب حالة العضوية للمستخدم {user_id} في المجموعة {group_id}: {e}")

    # إعداد رسالة التقرير
    msg = f"*نتائج التحقق للمجموعة `{group_id}`:*\n\n"

    if users_still_in_group:
        msg += "*المستخدمون الذين لا يزالون في المجموعة:* \n"
        for uid in users_still_in_group:
            msg += f"• `{uid}`\n"
        msg += "\n"
    else:
        msg += "*جميع المستخدمين المحذوفين غير موجودين في المجموعة.*\n\n"

    if users_not_in_group:
        msg += "*المستخدمون غير الموجودين في المجموعة:* \n"
        for uid in users_not_in_group:
            msg += f"• `{uid}`\n"
        msg += "\n"

    # إرسال تقرير التحقق إلى المستخدم المصرح له
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=escape_markdown(msg, version=2),
            parse_mode='MarkdownV2'
        )
        logger.info(f"تم الانتهاء من التحقق للمجموعة {group_id} بواسطة المستخدم {user.id}")
    except Exception as e:
        logger.error(f"خطأ في إرسال نتائج التحقق إلى المستخدم {user.id}: {e}")
        message = escape_markdown("⚠️ حدث خطأ أثناء إرسال نتائج التحقق.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        return

    # إلغاء عضوية المستخدمين الذين لا يزالون في المجموعة تلقائيًا
    if users_still_in_group:
        for uid in users_still_in_group:
            try:
                await context.bot.ban_chat_member(chat_id=group_id, user_id=uid)
                logger.info(f"تمت إزالة المستخدم {uid} من المجموعة {group_id} عبر أمر /check.")
            except Exception as e:
                logger.error(f"فشل في إزالة المستخدم {uid} من المجموعة {group_id}: {e}")

# ------------------- Handle Pending Removal -------------------

async def handle_pending_removal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع ردود المستخدم لإزالة مستخدم من قائمة "المستخدمين المحذوفين".
    يتوقع استقبال معرف المستخدم في الرسالة.
    """
    user = update.effective_user
    message_text = update.message.text.strip()
    logger.debug(f"تم استدعاء handle_pending_removal بواسطة المستخدم {user.id} مع الرسالة: {message_text}")
    
    if user.id not in pending_user_removals:
        # لا توجد عملية إزالة معلقة
        warning_message = escape_markdown("⚠️ لا توجد عملية إزالة معلقة. الرجاء استخدام الأمر المناسب.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=warning_message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"المستخدم {user.id} حاول إزالة مستخدم بدون عملية إزالة معلقة.")
        return
    
    group_id = pending_user_removals.pop(user.id)
    
    try:
        target_user_id = int(message_text)
    except ValueError:
        message = escape_markdown("⚠️ `user_id` يجب أن يكون عددًا صحيحًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"تم تقديم user_id غير صحيح إلى handle_pending_removal بواسطة المستخدم {user.id}: {message_text}")
        return
    
    # التحقق مما إذا كان المستخدم موجودًا في قائمة "المستخدمين المحذوفين" للمجموعة
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, target_user_id))
        if not c.fetchone():
            conn.close()
            message = escape_markdown(f"⚠️ المستخدم `{target_user_id}` غير موجود في قائمة 'المستخدمين المحذوفين' للمجموعة `{group_id}`.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"المستخدم {target_user_id} غير موجود في قائمة 'المستخدمين المحذوفين' للمجموعة {group_id} أثناء الإزالة بواسطة المستخدم {user.id}")
            return
        # المتابعة للإزالة
        c.execute('DELETE FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, target_user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        message = escape_markdown("⚠️ فشل في إزالة المستخدم من قائمة 'المستخدمين المحذوفين'. الرجاء المحاولة مرة أخرى لاحقًا.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"خطأ في إزالة المستخدم {target_user_id} من قائمة 'المستخدمين المحذوفين' للمجموعة {group_id}: {e}")
        return
    
    # إلغاء أذونات المستخدم إذا لزم الأمر
    try:
        revoke_user_permissions(target_user_id)
    except Exception as e:
        logger.error(f"خطأ في إلغاء أذونات المستخدم {target_user_id}: {e}")
        # ليس من الضروري إرسال رسالة؛ تم إزالة المستخدم من القائمة بالفعل
        # لذا يمكن المتابعة
    
    confirmation_message = escape_markdown(
        f"✅ تم إزالة المستخدم `{target_user_id}` من قائمة 'المستخدمين المحذوفين' للمجموعة `{group_id}`.",
        version=2
    )
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"تمت إزالة المستخدم {target_user_id} من قائمة 'المستخدمين المحذوفين' للمجموعة {group_id} بواسطة المستخدم {user.id}")
    except Exception as e:
        logger.error(f"خطأ في إرسال رسالة التأكيد لعملية إزالة المستخدم: {e}")

# ------------------- Message Handler Functions -------------------

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    حذف الرسائل التي تحتوي على نص عربي في المجموعات التي تم تفعيل الحذف فيها.
    """
    message = update.message
    if not message or not message.text:
        logger.debug("تم استقبال رسالة غير نصية أو فارغة.")
        return  # تجاهل الرسائل غير النصية أو الفارغة

    user = message.from_user
    chat = message.chat
    group_id = chat.id

    logger.debug(f"التحقق من الرسالة في المجموعة {group_id} من المستخدم {user.id}: {message.text}")

    # التحقق مما إذا كان الحذف مفعلًا لهذه المجموعة
    if not is_deletion_enabled(group_id):
        logger.debug(f"الحذف غير مفعل للمجموعة {group_id}.")
        return

    # التحقق مما إذا كان المستخدم يتجاوز
    if is_bypass_user(user.id):
        logger.debug(f"المستخدم {user.id} يتجاوز. لن يتم حذف الرسالة.")
        return

    # التحقق مما إذا كانت الرسالة تحتوي على نص عربي
    if is_arabic(message.text):
        try:
            await message.delete()
            logger.info(f"تم حذف رسالة عربية من المستخدم {user.id} في المجموعة {group_id}.")
            # تم إزالة رسالة التحذير لعدم إرسال إشعار للمستخدم
        except Exception as e:
            logger.error(f"خطأ في حذف الرسالة في المجموعة {group_id}: {e}")

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    حذف أي رسالة تُرسل إلى المجموعة إذا كان العلم مفعلًا.
    يشمل ذلك الرسائل من المستخدمين ورسائل النظام.
    """
    message = update.message
    if not message:
        return

    chat = message.chat
    group_id = chat.id

    # التحقق مما إذا كانت المجموعة مُعلّمة لحذف الرسائل
    if group_id in delete_all_messages_after_removal:
        try:
            await message.delete()
            logger.info(f"تم حذف رسالة في المجموعة {group_id}: {message.text or 'رسالة غير نصية.'}")
        except Exception as e:
            logger.error(f"فشل في حذف الرسالة في المجموعة {group_id}: {e}")

# ------------------- Utility Function -------------------

def is_arabic(text):
    """
    التحقق مما إذا كان النص يحتوي على أحرف عربية.
    """
    return bool(re.search(r'[\u0600-\u06FF]', text))

# ------------------- Error Handler -------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    التعامل مع الأخطاء التي تحدث أثناء التحديثات.
    """
    logger.error("حدث خطأ:", exc_info=context.error)

# ------------------- Main Function -------------------

def main():
    """
    الدالة الرئيسية لتهيئة البوت وتسجيل المعالجات.
    """
    try:
        init_db()
    except Exception as e:
        logger.critical(f"لا يمكن بدء البوت بسبب فشل تهيئة قاعدة البيانات: {e}")
        sys.exit(f"لا يمكن بدء البوت بسبب فشل تهيئة قاعدة البيانات: {e}")

    TOKEN = os.getenv('BOT_TOKEN')
    if not TOKEN:
        logger.error("⚠️ BOT_TOKEN غير مضبوط.")
        sys.exit("⚠️ BOT_TOKEN غير مضبوط.")
    TOKEN = TOKEN.strip()
    if TOKEN.lower().startswith('bot='):
        TOKEN = TOKEN[len('bot='):].strip()
        logger.warning("يجب ألا يحتوي BOT_TOKEN على بادئة 'bot='. تم إزالتها.")

    try:
        application = ApplicationBuilder().token(TOKEN).build()
    except Exception as e:
        logger.critical(f"فشل في بناء التطبيق باستخدام TOKEN المقدم: {e}")
        sys.exit(f"فشل في بناء التطبيق باستخدام TOKEN المقدم: {e}")

    # تسجيل معالجات الأوامر
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("group_add", group_add_cmd))
    application.add_handler(CommandHandler("rmove_group", rmove_group_cmd))
    application.add_handler(CommandHandler("bypass", bypass_cmd))
    application.add_handler(CommandHandler("unbypass", unbypass_cmd))
    application.add_handler(CommandHandler("group_id", group_id_cmd))
    application.add_handler(CommandHandler("show", show_groups_cmd))
    application.add_handler(CommandHandler("info", info_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("list", show_groups_cmd))  # افتراض أن /list مشابه لـ /show
    application.add_handler(CommandHandler("be_sad", be_sad_cmd))
    application.add_handler(CommandHandler("be_happy", be_happy_cmd))
    application.add_handler(CommandHandler("rmove_user", rmove_user_cmd))  # الأمر الموجود
    application.add_handler(CommandHandler("add_removed_user", add_removed_user_cmd))  # أمر جديد
    application.add_handler(CommandHandler("list_removed_users", list_removed_users_cmd))  # أمر جديد
    application.add_handler(CommandHandler("list_rmoved_rmove", list_rmoved_rmove_cmd))  # أمر جديد
    application.add_handler(CommandHandler("check", check_cmd))  # التأكد من وجود معالج واحد لـ /check

    # تسجيل معالجات الرسائل
    # 1. حذف الرسائل التي تحتوي على نص عربي
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_arabic_messages
    ))

    # 2. حذف أي رسائل أثناء تفعيل علم الحذف
    application.add_handler(MessageHandler(
        filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_any_messages
    ))

    # 3. التعامل مع الرسائل الخاصة لتعيين اسم المجموعة
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_private_message_for_group_name
    ))

    # 4. التعامل مع إزالة المستخدمين المعلقة
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_pending_removal
    ))

    # تسجيل معالج الأخطاء
    application.add_error_handler(error_handler)

    logger.info("🚀 بدء تشغيل البوت...")
    try:
        application.run_polling()
    except Exception as e:
        logger.critical(f"واجه البوت خطأً حرجًا ويتوقف: {e}")
        sys.exit(f"واجه البوت خطأً حرجًا ويتوقف: {e}")

if __name__ == '__main__':
    main()

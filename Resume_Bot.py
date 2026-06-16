# Standard library imports
import asyncio
import io
import os
import json
import signal
import time
import logging
from datetime import datetime
from threading import Thread
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

# Third-party imports
import psycopg2
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    MenuButtonCommands,
    ReplyKeyboardRemove  # Moved here from telegram.ext
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
    CallbackQueryHandler,
)
from psycopg2.pool import SimpleConnectionPool

# Local application imports
from premium_security import (
    generate_secure_key,
    validate_key_format,
    verify_key_signature,
    check_rate_limit,
    record_attempt,
    log_security_event,
    MAX_REDEEM_ATTEMPTS,
    REDEEM_COOLDOWN
)

import platform

# Add at the top with other imports
from aiohttp import web

# Add after connection_pool initialization
async def health_check(request):
    return web.Response(text="Bot is running")

async def run_webserver():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()
    print("Web server started")
    
# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global dictionary to store user data during the conversation
user_data = {}
redeem_attempts = {}

# Define states for conversation
NAME, CONTACT, EDUCATION, EXPERIENCE, SKILLS, SUMMARY = range(6)

# Template styles
TEMPLATES = {
    "BASIC": "📄 Basic (Free)",
    "MODERN": "💎 Modern (Premium)",
    "CREATIVE": "🎨 Creative (Premium)",
    "MINIMALIST": "✂️ Minimalist (Premium)",
}

# Fetch environment variables
TOKEN = os.getenv("TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise RuntimeError("Telegram bot TOKEN is missing.")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing.")

async def db_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin-only guard
    if str(update.effective_user.id) != ADMIN_ID:
        await update.message.reply_text("❌ You’re not allowed to use this command.")
        return

    # Existing logic
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            result = cur.fetchone()
            if result and result[0] == 1:
                await update.message.reply_text("✅ Database connection is healthy")
            else:
                await update.message.reply_text("⚠️ Database connection test failed")
    except Exception as e:
        await update.message.reply_text(f"❌ Database error: {e}")
    finally:
        if conn:
            put_db_connection(conn)


async def check_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin-only guard
    if str(update.effective_user.id) != ADMIN_ID:
        # use callback_query if coming from a button
        if update.callback_query:
            await update.callback_query.answer("❌ You’re not allowed to use this command.", show_alert=True)
        else:
            await update.message.reply_text("❌ You’re not allowed to use this command.")
        return

    user_id = update.effective_user.id
    state = context.user_data.get('_conversation_state')
    current_data = user_data.get(user_id, {})

    message = (
        f"🔄 Current State: {state}\n"
        f"📊 User Data: {json.dumps(current_data, indent=2)}"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(f"```{message}```", parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(f"```{message}```", parse_mode="MarkdownV2")



# Initialize connection pool
connection_pool = SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=DATABASE_URL,
    sslmode='require'
)


def get_db_connection():
    return connection_pool.getconn()

def put_db_connection(conn):
    connection_pool.putconn(conn)

def init_db():
    command = """
    CREATE TABLE IF NOT EXISTS premium_data (
        id SERIAL PRIMARY KEY,
        value TEXT UNIQUE NOT NULL,
        expiry_date DATE NOT NULL,
        is_key BOOLEAN NOT NULL
    );
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(command)
            conn.commit()
    except Exception as e:
        logger.error(f"DB Init Error: {e}")
        raise
    finally:
        if conn:
            put_db_connection(conn)


init_db()


def is_premium(user_id):
    if not user_id:
        return False

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT expiry_date FROM premium_data WHERE value = %s AND is_key = FALSE",
                (str(user_id),)
            )
            row = cur.fetchone()
            if row:
                expiry_date = row[0]
                return expiry_date > datetime.now().date()
            return False
    except Exception as e:
        logger.error(f"DB Premium Check Error: {e}")
        return False
    finally:
        if conn:
            put_db_connection(conn)

async def post_init(application):
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("newresume", "Create a new resume"),
        BotCommand("premium", "Premium features info"),
        BotCommand("redeem", "Redeem premium key"),
        BotCommand("help", "Get help"),
        BotCommand("privacy", "View privacy policy"),
        BotCommand("cancel", "Cancel current operation"),
    ]
    await application.bot.set_my_commands(commands)
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    asyncio.create_task(security_monitor(application))
    
    commands = await application.bot.get_my_commands()
    logger.info(f"Bot commands registered: {commands}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("✨ Create New Resume", callback_data="new_resume")],
        [InlineKeyboardButton("💎 Premium Features", callback_data="premium_features")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="show_help")],
        [InlineKeyboardButton("🔒 Privacy Policy", callback_data="privacy_policy")],  # New button
    ]

    user = update.effective_user
    greeting = (
        f"🌟 *Welcome to ResumeGenie*, {user.first_name}!\n\n"
        "I can help you create professional resumes in minutes. "
        "Choose an option below to get started."
    )

    premium_status = (
        "🌟 *Premium Status:* Active"
        if is_premium(user.id)
        else "🔒 *Premium Status:* Not Active"
    )

    if update.message:
        await update.message.reply_text(
            f"{greeting}\n\n{premium_status}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    else:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            f"{greeting}\n\n{premium_status}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "new_resume":
        # Instead of calling new_resume directly, send /newresume command
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="/newresume"
        )
        return  # Don't continue

    handlers = {
        "new_resume": new_resume,
        "premium_features": show_premium_features,
        "show_help": show_help,
        "back_to_main": start,
        "get_premium": get_premium,
        "privacy_policy": show_privacy_policy,  # New handler
    }

    if query.data in handlers:
        await handlers[query.data](update, context)
    elif query.data.startswith("template_"):
        template = query.data.split("_")[1]
        user_id = query.from_user.id
        if user_id not in user_data:
            user_data[user_id] = {}
        user_data[user_id]["template"] = template
        user_data[user_id]["user_id"] = user_id
        await query.edit_message_text(
            f"✅ Selected template: *{TEMPLATES[template]}*", parse_mode="Markdown"
        )
        # Run PDF generation as a background task to prevent callback query timeout
        asyncio.create_task(_generate_resume_background(update, context))


async def _generate_resume_background(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Background wrapper for generate_resume to prevent callback query timeout."""
    try:
        await generate_resume(update, context)
    except Exception as e:
        logger.error(f"Background resume generation error: {e}")
        try:
            if update.callback_query:
                await update.callback_query.message.reply_text(
                    "❌ Error generating your resume. Please try again."
                )
        except Exception:
            pass


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Help command triggered by {update.effective_user.id}")
    help_text = """
📝 *ResumeGenie Pro Help Guide* 📝

✨ *Getting Started*
- Use /start to see main menu
- Click "Create New Resume" to begin
- Follow the step-by-step process

💎 *Premium Features*
- Access premium templates
- Unlimited resume saves
- Priority support

🔑 *Premium Activation*
- Contact db for premium keys
- Use /redeem <key> to activate

🛠 *Commands*
/start - Show main menu
/newresume - Start new resume
/redeem - Activate premium
/cancel - Cancel current operation

Need more help? Contact @ThantLwinMaung
"""
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(help_text,
                                                     parse_mode="Markdown",
                                                     reply_markup=reply_markup)
    else:
        await update.message.reply_text(help_text,
                                        parse_mode="Markdown",
                                        reply_markup=reply_markup)

async def show_privacy_policy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    privacy_policy_url = "https://privacyforresumegenie.onrender.com"  # Replace with your actual URL
    
    keyboard = [
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main")],
    ]
    
    message = (
        "🔒 *Privacy Policy*\n\n"
        "We take your privacy seriously. Please read our privacy policy at:\n"
        f"[Privacy Policy Page]({privacy_policy_url})\n\n"
        "Key points:\n"
        "- We don't store your personal data\n"
        "- Your resume information is processed temporarily\n"
        "- No data sharing with third parties"
    )
    
    if query:
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    else:
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        
async def new_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query if update.callback_query else None
        user_id = update.effective_user.id

        # Initialize user data
        user_data[user_id] = {
            "name": "",
            "contact": "",
            "education": "",
            "experience": "",
            "skills": "",
            "summary": "",
            "template": "BASIC",
            "user_id": user_id,
        }

        message = (
            "📝 *Let's Create Your Professional Resume!*\n\n"
            "We'll go through a few simple steps to build your perfect resume.\n\n"
            "🔹 *Step 1 of 7*\n"
            "What's your *full name*?\n\n"
            "Example: *John Doe*"
        )

        if query:
            await query.edit_message_text(message, parse_mode="Markdown", reply_markup=None)
        else:
            await update.message.reply_text(
                message, 
                parse_mode="Markdown", 
                reply_markup=ReplyKeyboardRemove()
            )
            
        # Log the start of resume creation
        logger.info(f"Resume creation started for user {user_id}")
        return NAME
        
    except Exception as e:
        logger.error(f"Error in new_resume: {e}")
        error_msg = "❌ Failed to start resume creation. Please try again."
        if update.callback_query:
            await update.callback_query.answer(error_msg, show_alert=True)
        else:
            await update.message.reply_text(error_msg)
        return ConversationHandler.END


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        user_data[user_id]["name"] = update.message.text
        logger.info(f"User {user_id} provided name: {update.message.text}")

        await update.message.reply_text(
            "📞 *Step 2 of 7*\n"
            "Please share your *contact information*:\n\n"
            "Include any of these (separate with | ):\n"
            "- Email\n- Phone\n- LinkedIn\n- Portfolio\n\n"
            "Example:\n"
            "*john@email.com | +123456789 | linkedin.com/in/john*",
            parse_mode="Markdown",
        )
        return CONTACT
    except Exception as e:
        logger.error(f"Error in get_name: {e}")
        await update.message.reply_text("❌ Error processing your name. Please try again.")
        return ConversationHandler.END


async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["contact"] = update.message.text

    await update.message.reply_text(
        "🎓 *Step 3 of 7*\n"
        "Tell me about your *education*:\n\n"
        "Include:\n- Degree\n- University\n- Year\n\n"
        "Example:\n"
        "*BSc Computer Science, MIT, 2020*\n"
        "*MBA, Harvard University, 2022*",
        parse_mode="Markdown",
    )
    return EDUCATION


async def get_education(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["education"] = update.message.text

    await update.message.reply_text(
        "💼 *Step 4 of 7*\n"
        "List your *work experience*:\n\n"
        "For each position include:\n- Job Title\n- Company\n- Duration\n- Responsibilities\n\n"
        "Example:\n"
        "*Software Engineer, Google, 2020-Present*\n"
        "- Developed new features\n- Optimized performance",
        parse_mode="Markdown",
    )
    return EXPERIENCE


async def get_experience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["experience"] = update.message.text

    await update.message.reply_text(
        "🛠️ *Step 5 of 7*\n"
        "List your *skills* (comma separated):\n\n"
        "Example:\n"
        "*Python, JavaScript, Project Management, Team Leadership*",
        parse_mode="Markdown",
    )
    return SKILLS


async def get_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["skills"] = update.message.text

    await update.message.reply_text(
        "📝 *Step 6 of 7*\n"
        "Write a *professional summary* about yourself:\n\n"
        "Example:\n"
        "*Experienced software engineer with 5+ years in developing scalable web applications. "
        "Specialized in Python and cloud technologies. Strong problem-solving skills "
        "and team leadership experience.*",
        parse_mode="Markdown",
    )
    return SUMMARY


async def get_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["summary"] = update.message.text

    if is_premium(user_id):
        # Detailed example data for full one-page resume previews
        example_data = {
            "name": "Emily Chen",
            "contact": "emily.chen@email.com | (555) 123-4567 | linkedin.com/in/emilychen | github.com/emilychen",
            "education": (
                "MSc in Computer Science, Stanford University\n"
                "2018-2020 | GPA: 3.9/4.0\n"
                "Specialization: Artificial Intelligence\n\n"
                "BSc in Software Engineering, University of Toronto\n"
                "2014-2018 | Graduated with Honors"
            ),
            "experience": (
                "Senior Software Engineer, Tech Solutions Inc.\n"
                "2020-Present | San Francisco, CA\n"
                "- Lead team of 5 developers building scalable web applications\n"
                "- Designed architecture for customer portal serving 1M+ users\n"
                "- Reduced API response time by 40% through optimization\n\n"
                "Software Developer Intern, DataSystems Corp\n"
                "Summer 2019 | Mountain View, CA\n"
                "- Developed machine learning pipeline for data classification\n"
                "- Created automated testing framework saving 20+ hours/week"
            ),
            "skills": (
                "Programming: Python, JavaScript, Java, C++, SQL\n"
                "Frameworks: Django, React, TensorFlow, PyTorch\n"
                "Tools: Git, Docker, AWS, Kubernetes, Jenkins\n"
                "Languages: English (Fluent), Mandarin (Native)"
            ),
            "summary": (
                "Results-driven software engineer with 5+ years of experience in full-stack development "
                "and machine learning. Proven track record of designing and implementing scalable systems "
                "that handle millions of users. Strong leadership skills with experience mentoring junior "
                "developers. Passionate about creating efficient, maintainable code and solving complex "
                "technical challenges."
            ),
            "user_id": user_id,
        }

        # Create template selection
        keyboard = [
            [InlineKeyboardButton("📄 Basic", callback_data="template_BASIC")],
            [InlineKeyboardButton("💎 Modern", callback_data="template_MODERN")],
            [InlineKeyboardButton("🎨 Creative", callback_data="template_CREATIVE")],
            [InlineKeyboardButton("✂️ Minimalist", callback_data="template_MINIMALIST")],
        ]

        # Generate and send previews
        for template in TEMPLATES.keys():
            try:
                loop = asyncio.get_running_loop()
                pdf_bytes = await loop.run_in_executor(None, generate_pdf_bytes, {**example_data, "template": template})
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=io.BytesIO(pdf_bytes),
                    filename=f"{template}_preview.pdf",
                    caption=f"Preview: {TEMPLATES[template]}",
                )
            except Exception as e:
                print(f"Error generating {template} preview: {e}")
                await update.message.reply_text(
                    f"Couldn't generate {template} preview. Please try another template."
                )

        await update.message.reply_text(
            "🎨 *Choose your resume template*:\n\n"
            "Above you'll see previews of each template with example data.\n"
            "Select which one you'd like to use for your resume:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END
    else:
        user_data[user_id]["template"] = "BASIC"
        await update.message.reply_text(
            "⏳ Generating your resume with *Basic template*...\n\n"
            "Upgrade to premium for stylish templates!",
            parse_mode="Markdown",
        )
        return await generate_resume(update, context)


async def generate_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        # Note: query.answer() is already called by button_handler for template callbacks
        message = query.message
        user_id = query.from_user.id
    else:
        message = update.message
        user_id = update.effective_user.id

    # Ensure we have complete data
    if user_id not in user_data:
        await message.reply_text("❌ Error: Resume data not found. Please start again.")
        return ConversationHandler.END

    # Generate PDF in a thread pool to avoid blocking the async event loop
    try:
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(None, generate_pdf_bytes, user_data[user_id])
    except Exception as e:
        logger.error(f"PDF generation error: {e}")
        await message.reply_text("❌ Error generating resume. Please try again.")
        return ConversationHandler.END

    # Send to user
    await message.reply_document(
        document=io.BytesIO(pdf_bytes),
        filename=f"{user_data[user_id]['name']}_Resume.pdf",
        caption="✅ *Your professional resume is ready!*",
        parse_mode="Markdown",
    )

    # Clear user data after sending
    if user_id in user_data:
        del user_data[user_id]

    return ConversationHandler.END


def generate_pdf_bytes(data, preview_mode=False):

    from fpdf import FPDF

    class PDF(FPDF):
        def header(self):
            pass

        def footer(self):
            pass

    pdf = PDF()
    pdf.add_page()

    template = data.get("template", "BASIC")
    user_id = data.get("user_id")

    if template != "BASIC" and not preview_mode and not is_premium(user_id):
        template = "BASIC"

    if template == "BASIC":
        pdf.set_font("Arial", "B", 20)
        pdf.set_text_color(50, 50, 50)
        pdf.cell(0, 14, data["name"], 0, 1, "C")

        pdf.set_font("Arial", "", 10)
        contact_parts = [part.strip() for part in data["contact"].split("|")]
        contact_line = " | ".join(contact_parts)
        pdf.cell(0, 8, contact_line, 0, 1, "C")

        pdf.set_draw_color(160, 160, 160)
        pdf.set_line_width(0.4)
        pdf.line(15, pdf.get_y() + 4, 195, pdf.get_y() + 4)
        pdf.ln(12)

        sections = [
            ("PROFESSIONAL SUMMARY", data["summary"]),
            ("EDUCATION", data["education"]),
            ("WORK EXPERIENCE", data["experience"]),
            ("SKILLS", data["skills"]),
        ]

        for title, content in sections:
            pdf.set_font("Arial", "B", 14)
            pdf.set_text_color(70, 70, 70)
            pdf.cell(0, 8, title, 0, 1)
            pdf.set_draw_color(200, 200, 200)
            pdf.line(15, pdf.get_y() + 1, 195, pdf.get_y() + 1)
            pdf.ln(5)
            pdf.set_font("Arial", "", 11)
            pdf.set_text_color(30, 30, 30)
            pdf.multi_cell(0, 7, content)
            pdf.ln(8)

    elif template == "MODERN":
        pdf.set_fill_color(0, 102, 204)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Arial", "B", 22)
        pdf.cell(0, 14, data["name"], 0, 1, "C", True)

        pdf.set_font("Arial", "", 10)
        pdf.set_text_color(50, 50, 50)
        pdf.ln(5)
        contact_line = " | ".join([part.strip() for part in data["contact"].split("|")])
        pdf.cell(0, 8, contact_line, 0, 1, "C")
        pdf.ln(5)

        sections = [
            ("Summary", data["summary"]),
            ("Education", data["education"]),
            ("Experience", data["experience"]),
            ("Skills", data["skills"]),
        ]

        for title, content in sections:
            pdf.set_font("Arial", "B", 12)
            pdf.set_fill_color(230, 240, 255)
            pdf.set_text_color(0, 102, 204)
            pdf.cell(0, 8, f"  {title.upper()}", 0, 1, "L", True)
            pdf.set_font("Arial", "", 10)
            pdf.set_text_color(30, 30, 30)
            pdf.multi_cell(0, 6, content)
            pdf.ln(5)

    elif template == "CREATIVE":
        pdf.set_font("Arial", "B", 20)
        pdf.set_text_color(153, 0, 76)
        pdf.cell(0, 14, data["name"], 0, 1, "C")

        pdf.set_font("Arial", "I", 10)
        pdf.set_text_color(100, 100, 100)
        contact_line = " | ".join([part.strip() for part in data["contact"].split("|")])
        pdf.cell(0, 8, contact_line, 0, 1, "C")
        pdf.ln(5)

        sections = [
            ("About Me", data["summary"]),
            ("Learning Journey", data["education"]),
            ("Career Path", data["experience"]),
            ("Core Skills", data["skills"]),
        ]

        for title, content in sections:
            pdf.set_font("Arial", "B", 13)
            pdf.set_fill_color(255, 230, 240)
            pdf.set_text_color(204, 0, 102)
            pdf.cell(0, 8, f"  {title}", 0, 1, "L", True)
            pdf.set_font("Arial", "", 10)
            pdf.set_text_color(40, 40, 40)
            pdf.multi_cell(0, 6, content)
            pdf.ln(5)

    elif template == "MINIMALIST":
        pdf.set_font("Arial", "B", 18)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 12, data["name"], 0, 1, "C")

        pdf.set_font("Arial", "", 9)
        pdf.set_text_color(80, 80, 80)
        contact_line = " | ".join([part.strip() for part in data["contact"].split("|")])
        pdf.cell(0, 6, contact_line, 0, 1, "C")
        pdf.ln(6)

        sections = [
            ("Summary", data["summary"]),
            ("Education", data["education"]),
            ("Experience", data["experience"]),
            ("Skills", data["skills"]),
        ]

        for title, content in sections:
            pdf.set_font("Arial", "B", 11)
            pdf.set_fill_color(245, 245, 245)
            pdf.set_text_color(60, 60, 60)
            pdf.cell(0, 8, f"  {title}", 0, 1, "L", True)
            pdf.set_font("Arial", "", 10)
            pdf.set_text_color(50, 50, 50)
            pdf.multi_cell(0, 6, content)
            pdf.ln(4)

    if not is_premium(user_id):
        pdf.set_font("Arial", "I", 8)
        pdf.set_text_color(200, 200, 200)
        pdf.set_y(-10)
        pdf.cell(0, 10, "Created with ResumeGenie", 0, 0, "R")

    return pdf.output(dest="S").encode("latin1")


async def show_premium_features(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Premium command triggered by {update.effective_user.id}")
    premium_text = """
💎 *Premium Features* 💎

✨ *Professional Templates*
- Modern, Creative, and Minimalist designs
- ATS-friendly formats
- Custom color schemes

🔓 *Unlimited Access*
- No restrictions on resume saves
- Edit existing resumes anytime
- No watermarks on your resumes

⚡ *Priority Features*
- Faster processing
- Priority support
- Regular template updates

💰 *Pricing Plans*
- 1 month: 19,000 MMK
- 3 months: 50,000 MMK (15% off)
- 1 year: 159,600 MMK (30% off)

🔑 To activate premium:
1. Contact @ThantLwinMaung
2. Get your premium key
3. Use /redeem <key>
"""
    keyboard = [
        [InlineKeyboardButton("🛒 Get Premium", callback_data="get_premium")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(premium_text,
                                                     parse_mode="Markdown",
                                                     reply_markup=reply_markup)
    else:
        await update.message.reply_text(premium_text,
                                        parse_mode="Markdown",
                                        reply_markup=reply_markup)

    # Send example premium templates
    example_data = {
        "name": "Emily Chen",
        "contact": "emily.chen@email.com | (555) 123-4567 | linkedin.com/in/emilychen",
        "education": (
            "MSc in Computer Science, Stanford University\n"
            "2018-2020 | GPA: 3.9/4.0\n"
            "Specialization: Artificial Intelligence\n\n"
            "BSc in Software Engineering, University of Toronto\n"
            "2014-2018 | Graduated with Honors"
        ),
        "experience": (
            "Senior Software Engineer, Tech Solutions Inc.\n"
            "2020-Present | San Francisco, CA\n"
            "- Lead team of 5 developers building scalable web applications\n"
            "- Designed architecture for customer portal serving 1M+ users\n"
            "- Reduced API response time by 40% through optimization\n\n"
            "Software Developer Intern, DataSystems Corp\n"
            "Summer 2019 | Mountain View, CA\n"
            "- Developed machine learning pipeline for data classification\n"
            "- Created automated testing framework saving 20+ hours/week"
        ),
        "skills": (
            "Programming: Python, JavaScript, Java, C++, SQL\n"
            "Frameworks: Django, React, TensorFlow, PyTorch\n"
            "Tools: Git, Docker, AWS, Kubernetes, Jenkins\n"
            "Languages: English (Fluent), Mandarin (Native)"
        ),
        "summary": (
            "Results-driven software engineer with 5+ years of experience in full-stack development "
            "and machine learning. Proven track record of designing and implementing scalable systems "
            "that handle millions of users. Strong leadership skills with experience mentoring junior "
            "developers. Passionate about creating efficient, maintainable code and solving complex "
            "technical challenges."
        ),
        "user_id": update.effective_user.id,
    }

    # Send loading message
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔄 Preparing premium template examples...",
        parse_mode="Markdown",
    )

    # Generate and send previews of premium templates
    for template in ["MODERN", "CREATIVE", "MINIMALIST"]:
        try:
            loop = asyncio.get_running_loop()
            pdf_bytes = await loop.run_in_executor(None, generate_pdf_bytes, {**example_data, "template": template}, preview_mode=True)
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=io.BytesIO(pdf_bytes),
                filename=f"{template}_example.pdf",
                caption=f"Example: {TEMPLATES[template]}",
                parse_mode="Markdown",
            )

        except Exception as e:
            print(f"Error generating {template} example: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Couldn't generate {template} example. Please try again later.",
                parse_mode="Markdown",
            )

    # Add reminder for non-premium users
    if not is_premium(update.effective_user.id):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔓 *Upgrade to premium* to use these beautiful templates!\n\n"
            "Use /redeem with your premium key or contact @ThantLwinMaung to get started.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def get_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    contact_admin = "📩 Contact @ThantLwinMaung to get your premium key!"

    keyboard = [
        [InlineKeyboardButton("💎 Premium Features", callback_data="premium_features")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main")],
    ]

    await query.edit_message_text(
        f"🌟 *Get Premium Access*\n\n{contact_admin}\n\n"
        "After receiving your premium key, use:\n"
        "`/redeem YOUR_KEY` to activate premium.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def generate_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if str(update.effective_user.id) != ADMIN_ID:
            log_security_event("unauthorized_key_generation", str(update.effective_user.id))
            await update.message.reply_text("❌ Admin only command.")
            return

        duration = 30
        if context.args and context.args[0].isdigit():
            duration = min(int(context.args[0]), 365)

        key, expiry = generate_secure_key(duration)

        # Save key
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO premium_data (value, expiry_date, is_key) VALUES (%s, %s, TRUE)",
                (key, expiry)
            )
            conn.commit()
        put_db_connection(conn)

        log_security_event("key_generated", str(update.effective_user.id), f"Duration: {duration} days")

        await update.message.reply_text(
            f"🔑 *New Premium Key Generated*\n\n"
            f"Key: `{key}`\n"
            f"Duration: {duration} days\n"
            f"Expires: {expiry}\n\n"
            f"Share this with user:\n`/redeem {key}`",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"GenerateKey Error: {e}")
        await update.message.reply_text("❌ Failed to generate key. Please check logs.", parse_mode="Markdown")

async def redeem_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if check_rate_limit(user_id):
        remaining_time = int(REDEEM_COOLDOWN - (time.time() - redeem_attempts[user_id]['last_attempt']))
        await update.message.reply_text(
            f"⏳ Too many attempts! Please try again in {remaining_time} seconds.",
            parse_mode="Markdown",
        )
        return

    if not context.args or len(context.args) != 1:
        record_attempt(user_id, False)
        await update.message.reply_text(
            "Usage: `/redeem YOUR_KEY`\n\nContact @ThantLwinMaung to get a premium key.",
            parse_mode="Markdown",
        )
        return

    input_key = context.args[0].strip()
    if not validate_key_format(input_key):
        record_attempt(user_id, False)
        log_security_event("invalid_key_format", user_id, input_key)
        await update.message.reply_text(
            "❌ *Invalid Key Format*\n\nThe key you entered is not in the correct format.",
            parse_mode="Markdown",
        )
        return

    if not verify_key_signature(input_key):
        record_attempt(user_id, False)
        log_security_event("invalid_key_signature", user_id, input_key)
        await update.message.reply_text(
            "❌ *Invalid Key*\n\nThis key appears to be tampered with.",
            parse_mode="Markdown",
        )
        return

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT expiry_date FROM premium_data WHERE value = %s AND is_key = TRUE",
                (input_key,)
            )
            row = cur.fetchone()

            if not row:
                record_attempt(user_id, False)
                log_security_event("invalid_key_attempt", user_id, input_key)
                await update.message.reply_text(
                    "❌ *Invalid Key*\n\nThis key was not found in our system.",
                    parse_mode="Markdown",
                )
                return

            expiry_date = row[0]
            if expiry_date < datetime.now().date():
                record_attempt(user_id, False)
                log_security_event("expired_key", user_id, input_key)
                await update.message.reply_text(
                    "❌ *Expired Key*\n\nThis key has already expired.",
                    parse_mode="Markdown",
                )
                return

            # Save user as premium
            cur.execute(
                "INSERT INTO premium_data (value, expiry_date, is_key) VALUES (%s, %s, FALSE) "
                "ON CONFLICT (value) DO UPDATE SET expiry_date = EXCLUDED.expiry_date",
                (user_id, expiry_date)
            )

            # Delete the key
            cur.execute(
                "DELETE FROM premium_data WHERE value = %s AND is_key = TRUE",
                (input_key,)
            )

            conn.commit()
        put_db_connection(conn)

        record_attempt(user_id, True)
        log_security_event("key_redeemed", user_id, f"Expires: {expiry_date}")

        await update.message.reply_text(
            f"🎉 *Premium Activated!*\n\n"
            f"Your premium access is valid until *{expiry_date}*.\n\n"
            f"You now have access to all premium templates and features!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Premium Features", callback_data="premium_features")],
                [InlineKeyboardButton("✨ Create Resume", callback_data="new_resume")],
            ]),
        )

    except Exception as e:
        logger.error(f"RedeemKey Error: {e}")
        await update.message.reply_text("❌ Failed to redeem key. Please try again.", parse_mode="Markdown")

        
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_data:
        del user_data[user_id]

    await update.message.reply_text(
        "🚫 Operation cancelled. Your progress has been cleared.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error_msg = (
        f"⚠️ Error: {context.error}\n"
        f"Update: {update}\n"
        f"User: {update.effective_user if update else 'No update object'}"
    )
    
    # Print to console (visible in Render logs)
    print(error_msg)
    
    # Send error to admin
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"🚨 Bot Error:\n{error_msg}"
    )
    
    # Notify user
    if update and update.message:
        await update.message.reply_text("❌ An error occurred. Our team has been notified.")
    elif update and update.callback_query:
        await update.callback_query.answer("❌ Error occurred. Please try again.", show_alert=True)

async def security_monitor(context: ContextTypes.DEFAULT_TYPE):
    """Periodic security check"""
    while True:
        try:
            # Check for brute force attempts
            now = time.time()
            suspicious_users = [
                user_id for user_id, record in redeem_attempts.items()
                if record['attempts'] >= MAX_REDEEM_ATTEMPTS * 2
            ]
            
            if suspicious_users:
                message = "🚨 *Security Alert* 🚨\n\n"
                message += "Multiple failed redemption attempts detected:\n"
                for user_id in suspicious_users:
                    attempts = redeem_attempts[user_id]['attempts']
                    message += f"- User {user_id}: {attempts} failed attempts\n"
                
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=message,
                    parse_mode="Markdown"
                )
                
            # Sleep for 5 minutes between checks
            await asyncio.sleep(300)
            
        except Exception as e:
            print(f"Security monitor error: {e}")
            await asyncio.sleep(60)

async def shutdown(application):
    """Shutdown the bot gracefully"""
    await application.stop()
    await application.shutdown()
    # Close all database connections
    connection_pool.closeall()
    
def run_bot():
    """Run the Telegram bot in the background"""
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    setup_handlers(app)
    
    # Create an event loop for the thread
    def polling_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(app.run_polling())
    
    thread = Thread(target=polling_thread)
    thread.daemon = True
    thread.start()
    
    logger.info("✅ Telegram bot is running in background...")
    return thread

def setup_handlers(app):
    """Configure all handlers"""
    # Conversation handler first
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("newresume", new_resume),
            CallbackQueryHandler(new_resume, pattern="^new_resume$")
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
            EDUCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_education)],
            EXPERIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_experience)],
            SKILLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_skills)],
            SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_summary)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    # Add all handlers in order of priority
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("premium", show_premium_features))
    app.add_handler(CommandHandler("privacy", show_privacy_policy))
    app.add_handler(CommandHandler("generatekey", generate_key))
    app.add_handler(CommandHandler("redeem", redeem_key))
    app.add_handler(CommandHandler("dbcheck", db_check))
    app.add_handler(CommandHandler("state", check_state))
    app.add_handler(CommandHandler("cancel", cancel))
    # Explicit cancel handler
    # Only ADMIN_ID can call /dbcheck
    app.add_handler(
        CommandHandler(
            "dbcheck",
            db_check,
            filters=filters.User(user_id=int(ADMIN_ID))
        )
    )

    # Only ADMIN_ID can call /state
    app.add_handler(
        CommandHandler(
            "state",
            check_state,
            filters=filters.User(user_id=int(ADMIN_ID))
        )
    )
    app.add_handler(
        CommandHandler(
            "generatekey",
            generate_key,
            filters=filters.User(user_id=int(ADMIN_ID))
        )
    )


    # Add conversation handler
    app.add_handler(conv_handler)
    
    # Callback query handler should come after command handlers
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Error handler last
    app.add_error_handler(error_handler)
    
async def main():
    # Start web server
    asyncio.create_task(run_webserver())
    
    # Start Telegram bot with longer timeouts for PDF generation
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    setup_handlers(app)
    
    logger.info("✅ Starting services...")
    await app.initialize()
    await app.start()
    
    if app.updater:
        await app.updater.start_polling()
    
    # Keep running
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        main_task = loop.create_task(main())

        def shutdown_handler():
            logger.info("Signal received, initiating shutdown...")
            main_task.cancel()

        if platform.system() != "Windows":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, shutdown_handler)
        else:
            logger.warning("Signal handlers are not supported on Windows. Skipping...")

        loop.run_forever()

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        import traceback
        logger.error("Bot crashed:\n%s", traceback.format_exc())
    finally:
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()

        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        logger.info("✅ Fully shut down")
# Standard library imports
import os
import json
import uuid
import io
import time
import asyncio
from datetime import datetime, timedelta
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

# Third-party imports
import psycopg2
from flask import Flask, request, jsonify
from fpdf import FPDF
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    MenuButtonCommands,
    BotCommand,
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

# Initialize Flask app
flask_app = Flask(__name__)

# Global dictionary to store user data during the conversation
user_data = {}

# Define states for conversation as simple integers
NAME, CONTACT, EDUCATION, EXPERIENCE, SKILLS, SUMMARY, TEMPLATE = range(7)

# Template styles with emoji icons
TEMPLATES = {
    "BASIC": "📄 Basic (Free)",
    "MODERN": "💎 Modern (Premium)",
    "CREATIVE": "🎨 Creative (Premium)",
    "MINIMALIST": "✂️ Minimalist (Premium)",
}

# Fetch environment variables
TOKEN = os.getenv("TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
PORT = int(os.getenv("PORT", 10000))  # Default to 10000 if not set
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise RuntimeError("Telegram bot TOKEN is missing. Please set it as an environment variable.")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing. Please set it as an environment variable.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing. Please set it as an environment variable.")

# Add Flask route for health check
@flask_app.route('/')
def health_check():
    return jsonify({"status": "ok", "message": "ResumeGenie bot is running", "port": PORT})

# Database connection helper
def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

# Initialize database tables
def init_db():
    commands = (
        """
        CREATE TABLE IF NOT EXISTS premium_data (
            id SERIAL PRIMARY KEY,
            keys JSONB NOT NULL DEFAULT '{}'::jsonb,
            premium_users JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """,
        """INSERT INTO premium_data (id, keys, premium_users)
           VALUES (1, '{}', '{}')
           ON CONFLICT (id) DO NOTHING"""
    )
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        for command in commands:
            cur.execute(command)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"⚠️ DB Init Error: {e}")
        raise
    finally:
        if conn:
            conn.close()

# Initialize the database
init_db()

def load_db():
    """Load all data from PostgreSQL"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT keys, premium_users FROM premium_data WHERE id = 1")
        result = cur.fetchone()
        return {
            "keys": result[0] if result else {},
            "premium_users": result[1] if result else {}
        }
    except Exception as e:
        print(f"⚠️ DB Load Error: {e}")
        return {"keys": {}, "premium_users": {}}
    finally:
        if conn:
            conn.close()

def save_db(data):
    """Save data to PostgreSQL"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE premium_data 
            SET keys = %s, premium_users = %s
            WHERE id = 1
        """, (json.dumps(data.get("keys", {})), 
              json.dumps(data.get("premium_users", {}))))
        conn.commit()
    except Exception as e:
        print(f"🚨 Critical DB Save Error: {e}")
        # Send alert to admin
        asyncio.run(context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Database write failed!\nError: {e}"
        ))
        raise
    finally:
        if conn:
            conn.close()

def is_premium(user_id):
    if not user_id:
        return False

    db = load_db()
    user_id_str = str(user_id)

    if user_id_str in db["premium_users"]:
        expiry_date = db["premium_users"][user_id_str]
        try:
            expiry = datetime.strptime(expiry_date, "%Y-%m-%d")
            return expiry > datetime.now()
        except ValueError:
            return False
    return False


async def post_init(application):
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("newresume", "Create a new resume"),
        BotCommand("premium", "Premium features info"),
        BotCommand("redeem", "Redeem premium key"),
        BotCommand("help", "Get help"),
        BotCommand("privacy", "View privacy policy"),  # New command
        BotCommand("cancel", "Cancel current operation"),
    ]
    await application.bot.set_my_commands(commands)
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    asyncio.create_task(security_monitor(application))


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

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_premium_features(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_help(update, context)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

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
        await generate_resume(update, context)


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

Need more help? Contact @techadmin009
"""

    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_main")]]

    query = update.callback_query
    await query.edit_message_text(
        help_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )

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
            message, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
        )
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["name"] = update.message.text

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
                pdf_bytes = generate_pdf_bytes({**example_data, "template": template})
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
        await query.answer()
        message = query.message
        user_id = query.from_user.id
    else:
        message = update.message
        user_id = update.effective_user.id

    # Ensure we have complete data
    if user_id not in user_data:
        await message.reply_text("❌ Error: Resume data not found. Please start again.")
        return ConversationHandler.END

    # Generate PDF
    try:
        pdf_bytes = generate_pdf_bytes(user_data[user_id])
    except Exception as e:
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


def generate_pdf_bytes(data):
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

    if template != "BASIC" and not is_premium(user_id):
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
    query = update.callback_query
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
    1. Contact @techadmin009
    2. Get your premium key
    3. Use /redeem <key>
    """

    keyboard = [
        [InlineKeyboardButton("🛒 Get Premium", callback_data="get_premium")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main")],
    ]

    await query.edit_message_text(
        premium_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )

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
            pdf_bytes = generate_pdf_bytes({**example_data, "template": template})
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
            "Use /redeem with your premium key or contact @techadmin009 to get started.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def get_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    contact_admin = "📩 Contact @techadmin009 to get your premium key!"

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
        admin_id = str(ADMIN_ID)
        user_id = str(update.effective_user.id)
        
        print(f"User ID trying to generate key: {user_id} (type: {type(user_id)})")
        print(f"Admin ID: {admin_id} (type: {type(admin_id)})")
        
        if user_id != admin_id:
            log_security_event("unauthorized_key_generation", user_id)
            await update.message.reply_text("❌ Admin only command.")
            return

        duration = 30
        if context.args and context.args[0].isdigit():
            duration = int(context.args[0])
            if duration > 365:
                duration = 365

        key, expiry = generate_secure_key(duration)

        db = load_db()
        db["keys"][key] = expiry
        save_db(db)

        log_security_event("key_generated", user_id, f"Duration: {duration} days")

        await update.message.reply_text(
            f"🔑 *New Secure Premium Key Generated*\n\n"
            f"Key: `{key}`\n"
            f"Duration: {duration} days\n"
            f"Expires: {expiry}\n\n"
            f"User can redeem with:\n`/redeem {key}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"GenerateKey Error: {e}")
        await update.message.reply_text(
            "❌ Failed to generate key. Please check logs.",
            parse_mode="Markdown"
        )

async def redeem_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Check rate limiting
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
            "Usage: `/redeem YOUR_KEY`\n\n"
            "Contact @techadmin009 to get a premium key.",
            parse_mode="Markdown",
        )
        return

    key = context.args[0].strip().upper()
    db = load_db()

    # Validate key format first
    if not validate_key_format(key):
        record_attempt(user_id, False)
        log_security_event("invalid_key_format", user_id, key)
        await update.message.reply_text(
            "❌ *Invalid Key Format*\n\n"
            "The key format is incorrect. Please check and try again.",
            parse_mode="Markdown",
        )
        return

    # Verify key signature
    if not verify_key_signature(key):
        record_attempt(user_id, False)
        log_security_event("invalid_key_signature", user_id, key)
        await update.message.reply_text(
            "❌ *Invalid Key*\n\n"
            "The key verification failed. It may be corrupted.",
            parse_mode="Markdown",
        )
        return

    if key in db["keys"]:
        expiry_date = db["keys"][key]

        try:
            expiry = datetime.strptime(expiry_date, "%Y-%m-%d")
            if expiry < datetime.now():
                record_attempt(user_id, False)
                log_security_event("expired_key", user_id, key)
                await update.message.reply_text(
                    "❌ *Expired Key*\n\nThis key has already expired.",
                    parse_mode="Markdown",
                )
                return
        except ValueError:
            record_attempt(user_id, False)
            log_security_event("invalid_expiry_format", user_id, key)
            await update.message.reply_text(
                "❌ *Invalid Key Format*\n\n"
                "This key is corrupted. Please contact admin.",
                parse_mode="Markdown",
            )
            return

        # All checks passed - redeem key
        db["premium_users"][user_id] = expiry_date
        del db["keys"][key]
        save_db(db)
        record_attempt(user_id, True)
        log_security_event("key_redeemed", user_id, f"Expires: {expiry_date}")

        await update.message.reply_text(
            f"🎉 *Premium Activated!*\n\n"
            f"Your premium access is valid until *{expiry_date}*.\n\n"
            f"You now have access to all premium features!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Premium Features", callback_data="premium_features")],
                [InlineKeyboardButton("✨ Create Resume", callback_data="new_resume")],
            ]),
        )
    else:
        record_attempt(user_id, False)
        log_security_event("invalid_key_attempt", user_id, key)
        await update.message.reply_text(
            "❌ *Invalid Key*\n\n"
            "The key you entered is invalid or has expired.\n"
            "Contact @techadmin009 for assistance.",
            parse_mode="Markdown",
        )

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

def run_flask():
    from gunicorn.app.base import BaseApplication
    
    class FlaskApplication(BaseApplication):
        def __init__(self, app, options=None):
            self.application = app
            self.options = options or {}
            super().__init__()

        def load_config(self):
            for key, value in self.options.items():
                self.cfg.set(key.lower(), value)

        def load(self):
            return self.application

    options = {
        'bind': f'0.0.0.0:{PORT}',
        'workers': 4,
        'threads': 2,
    }
    FlaskApplication(flask_app, options).run()

# Standard library imports
import os
import json
import uuid
import io
import time
import asyncio
from datetime import datetime, timedelta
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

# Third-party imports
import psycopg2
from flask import Flask, request, jsonify
from fpdf import FPDF
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    MenuButtonCommands,
    BotCommand,
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

# Initialize Flask app
flask_app = Flask(__name__)

# Global dictionary to store user data during the conversation
user_data = {}

# Define states for conversation as simple integers
NAME, CONTACT, EDUCATION, EXPERIENCE, SKILLS, SUMMARY, TEMPLATE = range(7)

# Template styles with emoji icons
TEMPLATES = {
    "BASIC": "📄 Basic (Free)",
    "MODERN": "💎 Modern (Premium)",
    "CREATIVE": "🎨 Creative (Premium)",
    "MINIMALIST": "✂️ Minimalist (Premium)",
}

# Fetch environment variables
TOKEN = os.getenv("TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
PORT = int(os.getenv("PORT", 10000))  # Default to 10000 if not set
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise RuntimeError("Telegram bot TOKEN is missing. Please set it as an environment variable.")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing. Please set it as an environment variable.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing. Please set it as an environment variable.")

# Add Flask route for health check
@flask_app.route('/')
def health_check():
    return jsonify({"status": "ok", "message": "ResumeGenie bot is running", "port": PORT})

# Database connection helper
def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

# Initialize database tables
def init_db():
    commands = (
        """
        CREATE TABLE IF NOT EXISTS premium_data (
            id SERIAL PRIMARY KEY,
            keys JSONB NOT NULL DEFAULT '{}'::jsonb,
            premium_users JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """,
        """INSERT INTO premium_data (id, keys, premium_users)
           VALUES (1, '{}', '{}')
           ON CONFLICT (id) DO NOTHING"""
    )
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        for command in commands:
            cur.execute(command)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"⚠️ DB Init Error: {e}")
        raise
    finally:
        if conn:
            conn.close()

# Initialize the database
init_db()

def load_db():
    """Load all data from PostgreSQL"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT keys, premium_users FROM premium_data WHERE id = 1")
        result = cur.fetchone()
        return {
            "keys": result[0] if result else {},
            "premium_users": result[1] if result else {}
        }
    except Exception as e:
        print(f"⚠️ DB Load Error: {e}")
        return {"keys": {}, "premium_users": {}}
    finally:
        if conn:
            conn.close()

def save_db(data):
    """Save data to PostgreSQL"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE premium_data 
            SET keys = %s, premium_users = %s
            WHERE id = 1
        """, (json.dumps(data.get("keys", {})), 
              json.dumps(data.get("premium_users", {}))))
        conn.commit()
    except Exception as e:
        print(f"🚨 Critical DB Save Error: {e}")
        # Send alert to admin
        asyncio.run(context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Database write failed!\nError: {e}"
        ))
        raise
    finally:
        if conn:
            conn.close()

def is_premium(user_id):
    if not user_id:
        return False

    db = load_db()
    user_id_str = str(user_id)

    if user_id_str in db["premium_users"]:
        expiry_date = db["premium_users"][user_id_str]
        try:
            expiry = datetime.strptime(expiry_date, "%Y-%m-%d")
            return expiry > datetime.now()
        except ValueError:
            return False
    return False

async def run_bot():
    """Run the Telegram bot"""
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("newresume", new_resume),
            CallbackQueryHandler(new_resume, pattern="^new_resume$"),
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
            EDUCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_education)],
            EXPERIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_experience)],
            SKILLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_skills)],
            SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_summary)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("privacy", show_privacy_policy))
    app.add_handler(CommandHandler("generatekey", generate_key))
    app.add_handler(CommandHandler("redeem", redeem_key))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    print("✅ Telegram bot is running...")
    await app.run_polling()
    
async def main():
    """Main async function to run both services"""
    # Run Flask in a separate thread
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Run the bot
    await run_bot()

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()

    # Start the main async function
    asyncio.run(main())
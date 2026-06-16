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

# ===== LANGUAGE SUPPORT =====
# Available languages
LANGUAGES = {
    "en": "English 🇬🇧",
    "my": "မြန်မာ 🇲🇲",
}

# User language preference storage: {user_id: language_code}
user_languages = {}

def get_user_lang(user_id):
    """Get the language code for a user, defaulting to 'en'"""
    return user_languages.get(user_id, "en")

def set_user_lang(user_id, lang_code):
    """Set the language preference for a user"""
    if lang_code in LANGUAGES:
        user_languages[user_id] = lang_code
        return True
    return False

def t(user_id, key, **kwargs):
    """
    Translate a key into the user's preferred language.
    Falls back to English if the key or translation is missing.
    """
    lang = get_user_lang(user_id)
    text = TRANSLATIONS.get(key, {}).get(lang)
    if text is None:
        # Fallback to English
        text = TRANSLATIONS.get(key, {}).get("en", key)
    if kwargs:
        text = text.format(**kwargs)
    return text

# Full translations dictionary
TRANSLATIONS = {
    # ===== START / WELCOME =====
    "welcome": {
        "en": (
            "🌟 *Welcome to ResumeGenie*, {name}!\n\n"
            "I can help you create professional resumes in minutes. "
            "Choose an option below to get started."
        ),
        "my": (
            "🌟 *ResumeGenie မှကြိုဆိုပါတယ်*, {name}!\n\n"
            "ကျွန်ုပ်သည် သင့်အတွက် မိနစ်ပိုင်းအတွင်း ပရော်ဖက်ရှင်နယ် CV များကို "
            "ဖန်တီးပေးနိုင်ပါသည်။ စတင်ရန် အောက်ပါရွေးချယ်စရာများထဲမှ တစ်ခုကို ရွေးချယ်ပါ။"
        ),
    },
    "premium_status_active": {
        "en": "🌟 *Premium Status:* Active",
        "my": "🌟 *Premium အခြေအနေ:* အသက်ဝင်နေသည်",
    },
    "premium_status_inactive": {
        "en": "🔒 *Premium Status:* Not Active",
        "my": "🔒 *Premium အခြေအနေ:* မသက်ဝင်သေးပါ",
    },

    # ===== BUTTON LABELS =====
    "btn_new_resume": {
        "en": "✨ Create New Resume",
        "my": "✨ CV အသစ်ဖန်တီးမည်",
    },
    "btn_premium": {
        "en": "💎 Premium Features",
        "my": "💎 Premium လုပ်ဆောင်ချက်များ",
    },
    "btn_help": {
        "en": "ℹ️ Help",
        "my": "ℹ️ အကူအညီ",
    },
    "btn_privacy": {
        "en": "🔒 Privacy Policy",
        "my": "🔒 ကိုယ်ရေးအချက်အလက်မူဝါဒ",
    },
    "btn_back": {
        "en": "⬅️ Back",
        "my": "⬅️ နောက်သို့",
    },
    "btn_get_premium": {
        "en": "🛒 Get Premium",
        "my": "🛒 Premium ရယူမည်",
    },
    "btn_language": {
        "en": "🌐 Language",
        "my": "🌐 ဘာသာစကား",
    },

    # ===== RESUME CREATION =====
    "resume_start_title": {
        "en": "📝 *Let's Create Your Professional Resume!*",
        "my": "📝 *သင့်ရဲ့ Professional CV ကိုဖန်တီးကြရအောင်!*",
    },
    "resume_start_desc": {
        "en": "We'll go through a few simple steps to build your perfect resume.",
        "my": "သင့်ရဲ့အကောင်းဆုံး CV ကိုဖန်တီးဖို့ အဆင့်အနည်းငယ်ကို ဖြတ်သန်းသွားပါမယ်။",
    },
    "resume_step_name": {
        "en": "🔹 *Step 1 of 7*\nWhat's your *full name*?\n\nExample: *John Doe*",
        "my": "🔹 *အဆင့် ၁ မှ ၇*\nသင့်ရဲ့ *နာမည်အပြည့်အစုံ* ကိုထည့်ပါ။\n\nဥပမာ: *မောင်ကျော်မြင့်*",
    },
    "resume_step_contact": {
        "en": (
            "📞 *Step 2 of 7*\n"
            "Please share your *contact information*:\n\n"
            "Include any of these (separate with | ):\n"
            "- Email\n- Phone\n- LinkedIn\n- Portfolio\n\n"
            "Example:\n"
            "*john@email.com | +123456789 | linkedin.com/in/john*"
        ),
        "my": (
            "📞 *အဆင့် ၂ မှ ၇*\n"
            "သင့်ရဲ့ *ဆက်သွယ်ရန်အချက်အလက်များ* ကိုမျှဝေပါ:\n\n"
            "ဤအရာများထည့်နိုင်သည် (| ဖြင့်ခြားပါ):\n"
            "- အီးမေးလ်\n- ဖုန်းနံပါတ်\n- LinkedIn\n- Portfolio\n\n"
            "ဥပမာ:\n"
            "*john@email.com | +123456789 | linkedin.com/in/john*"
        ),
    },
    "resume_step_education": {
        "en": (
            "🎓 *Step 3 of 7*\n"
            "Tell me about your *education*:\n\n"
            "Include:\n- Degree\n- University\n- Year\n\n"
            "Example:\n"
            "*BSc Computer Science, MIT, 2020*\n"
            "*MBA, Harvard University, 2022*"
        ),
        "my": (
            "🎓 *အဆင့် ၃ မှ ၇*\n"
            "သင့်ရဲ့ *ပညာရေး* အကြောင်းပြောပြပါ:\n\n"
            "ထည့်သွင်းရန်:\n- ဘွဲ့အမည်\n- တက္ကသိုလ်\n- ခုနှစ်\n\n"
            "ဥပမာ:\n"
            "*BSc Computer Science, MIT, 2020*\n"
            "*MBA, Harvard University, 2022*"
        ),
    },
    "resume_step_experience": {
        "en": (
            "💼 *Step 4 of 7*\n"
            "List your *work experience*:\n\n"
            "For each position include:\n- Job Title\n- Company\n- Duration\n- Responsibilities\n\n"
            "Example:\n"
            "*Software Engineer, Google, 2020-Present*\n"
            "- Developed new features\n- Optimized performance"
        ),
        "my": (
            "💼 *အဆင့် ၄ မှ ၇*\n"
            "သင့်ရဲ့ *အလုပ်အတွေ့အကြုံ* ကိုစာရင်းပြုစုပါ:\n\n"
            "ရာထူးတစ်ခုစီအတွက်:\n- ရာထူးအမည်\n- ကုမ္ပဏီ\n- ကာလ\n- တာဝန်များ\n\n"
            "ဥပမာ:\n"
            "*Software Engineer, Google, 2020-ယနေ့ထိ*\n"
            "- အင်္ဂါရပ်အသစ်များတီထွင်ခြင်း\n- စွမ်းဆောင်ရည်မြှင့်တင်ခြင်း"
        ),
    },
    "resume_step_skills": {
        "en": (
            "🛠️ *Step 5 of 7*\n"
            "List your *skills* (comma separated):\n\n"
            "Example:\n"
            "*Python, JavaScript, Project Management, Team Leadership*"
        ),
        "my": (
            "🛠️ *အဆင့် ၅ မှ ၇*\n"
            "သင့်ရဲ့ *ကျွမ်းကျင်မှုများ* ကိုစာရင်းပြုစုပါ (ကော်မာခြားပါ):\n\n"
            "ဥပမာ:\n"
            "*Python, JavaScript, စီမံကိန်းစီမံခန့်ခွဲမှု, အဖွဲ့ခေါင်းဆောင်မှု*"
        ),
    },
    "resume_step_summary": {
        "en": (
            "📝 *Step 6 of 7*\n"
            "Write a *professional summary* about yourself:\n\n"
            "Example:\n"
            "*Experienced software engineer with 5+ years in developing scalable web applications. "
            "Specialized in Python and cloud technologies. Strong problem-solving skills "
            "and team leadership experience.*"
        ),
        "my": (
            "📝 *အဆင့် ၆ မှ ၇*\n"
            "သင့်အကြောင်း *အနှစ်ချုပ်* ရေးသားပါ:\n\n"
            "ဥပမာ:\n"
            "*၅ နှစ်ကျော်အတွေ့အကြုံရှိတဲ့ Software Engineer တစ်ဦးဖြစ်ပြီး "
            "Python နှင့် Cloud နည်းပညာများကို အထူးပြုထားပါသည်။ "
            "ပြဿနာဖြေရှင်းနိုင်မှုနှင့် အဖွဲ့ခေါင်းဆောင်မှု အတွေ့အကြုံများရှိပါသည်။*"
        ),
    },
    "resume_template_choose": {
        "en": (
            "🎨 *Choose your resume template*:\n\n"
            "Above you'll see previews of each template with example data.\n"
            "Select which one you'd like to use for your resume:"
        ),
        "my": (
            "🎨 *သင့် CV အတွက် Template ကိုရွေးချယ်ပါ*:\n\n"
            "အထက်တွင် Template တစ်ခုစီ၏ နမူနာများကို ကြည့်ရှုနိုင်ပါသည်။\n"
            "သင်အသုံးပြုလိုသော Template ကိုရွေးချယ်ပါ:"
        ),
    },
    "resume_generating": {
        "en": "⏳ Generating your resume with *{template} template*...\n\nUpgrade to premium for stylish templates!",
        "my": "⏳ *{template} Template* ဖြင့် သင့် CV ကိုဖန်တီးနေပါသည်...\n\nစတိုင်ကျသော Template များအတွက် Premium သို့အဆင့်မြှင့်ပါ!",
    },
    "resume_ready": {
        "en": "✅ *Your professional resume is ready!*",
        "my": "✅ *သင့်ရဲ့ Professional CV အဆင်သင့်ဖြစ်ပါပြီ!*",
    },
    "resume_error_data": {
        "en": "❌ Error: Resume data not found. Please start again.",
        "my": "❌ အမှား: CV ဒေတာကိုရှာမတွေ့ပါ။ ကျေးဇူးပြု၍ ပြန်လည်စတင်ပါ။",
    },
    "resume_error_generate": {
        "en": "❌ Error generating resume. Please try again.",
        "my": "❌ CV ဖန်တီးရာတွင် အမှားရှိနေပါသည်။ ကျေးဇူးပြု၍ ပြန်လည်ကြိုးစားပါ။",
    },
    "resume_template_selected": {
        "en": "✅ Selected template: *{template}*",
        "my": "✅ ရွေးချယ်ထားသော Template: *{template}*",
    },

    # ===== HELP =====
    "help_title": {
        "en": "📝 *ResumeGenie Pro Help Guide* 📝",
        "my": "📝 *ResumeGenie Pro အကူအညီလမ်းညွှန်* 📝",
    },
    "help_getting_started_title": {
        "en": "✨ *Getting Started*",
        "my": "✨ *စတင်အသုံးပြုခြင်း*",
    },
    "help_getting_started": {
        "en": (
            "- Use /start to see main menu\n"
            "- Click \"Create New Resume\" to begin\n"
            "- Follow the step-by-step process"
        ),
        "my": (
            "- ပင်မမီနူးကြည့်ရန် /start ကိုသုံးပါ\n"
            "- စတင်ရန် \"CV အသစ်ဖန်တီးမည်\" ကိုနှိပ်ပါ\n"
            "- အဆင့်ဆင့်လုပ်ဆောင်ချက်များကိုလိုက်နာပါ"
        ),
    },
    "help_premium_title": {
        "en": "💎 *Premium Features*",
        "my": "💎 *Premium လုပ်ဆောင်ချက်များ*",
    },
    "help_premium": {
        "en": (
            "- Access premium templates\n"
            "- Unlimited resume saves\n"
            "- Priority support"
        ),
        "my": (
            "- Premium Template များကိုသုံးခွင့်\n"
            "- CV သိမ်းဆည်းမှု အကန့်အသတ်မရှိ\n"
            "- ဦးစားပေးပံ့ပိုးကူညီမှု"
        ),
    },
    "help_activation_title": {
        "en": "🔑 *Premium Activation*",
        "my": "🔑 *Premium အသက်သွင်းခြင်း*",
    },
    "help_activation": {
        "en": (
            "- Contact admin for premium keys\n"
            "- Use /redeem <key> to activate"
        ),
        "my": (
            "- Premium သော့များအတွက် အက်ဒမင်ကိုဆက်သွယ်ပါ\n"
            "- အသက်သွင်းရန် /redeem <key> ကိုသုံးပါ"
        ),
    },
    "help_commands_title": {
        "en": "🛠 *Commands*",
        "my": "🛠 *Commands များ*",
    },
    "help_commands": {
        "en": (
            "/start - Show main menu\n"
            "/newresume - Start new resume\n"
            "/redeem - Activate premium\n"
            "/language - Change language\n"
            "/cancel - Cancel current operation"
        ),
        "my": (
            "/start - ပင်မမီနူးပြသခြင်း\n"
            "/newresume - CV အသစ်စတင်ခြင်း\n"
            "/redeem - Premium အသက်သွင်းခြင်း\n"
            "/language - ဘာသာစကားပြောင်းလဲခြင်း\n"
            "/cancel - လုပ်ဆောင်ချက်ဖျက်သိမ်းခြင်း"
        ),
    },
    "help_contact": {
        "en": "Need more help? Contact @ThantLwinMaung",
        "my": "နောက်ထပ်အကူအညီလိုပါသလား? @ThantLwinMaung ကိုဆက်သွယ်ပါ",
    },

    # ===== PREMIUM FEATURES =====
    "premium_title": {
        "en": "💎 *Premium Features* 💎",
        "my": "💎 *Premium လုပ်ဆောင်ချက်များ* 💎",
    },
    "premium_templates_title": {
        "en": "✨ *Professional Templates*",
        "my": "✨ *Professional Template များ*",
    },
    "premium_templates": {
        "en": (
            "- Modern, Creative, and Minimalist designs\n"
            "- ATS-friendly formats\n"
            "- Custom color schemes"
        ),
        "my": (
            "- Modern, Creative နှင့် Minimalist ဒီဇိုင်းများ\n"
            "- ATS-friendly ဖော်မတ်များ\n"
            "- အရောင်အသွေးများကိုယ်တိုင်ပြောင်းလဲနိုင်"
        ),
    },
    "premium_unlimited_title": {
        "en": "🔓 *Unlimited Access*",
        "my": "🔓 *အကန့်အသတ်မရှိအသုံးပြုခွင့်*",
    },
    "premium_unlimited": {
        "en": (
            "- No restrictions on resume saves\n"
            "- Edit existing resumes anytime\n"
            "- No watermarks on your resumes"
        ),
        "my": (
            "- CV သိမ်းဆည်းခြင်းကိုကန့်သတ်ချက်မရှိ\n"
            "- ရှိပြီးသား CV များကိုအချိန်မရွေးပြင်ဆင်နိုင်\n"
            "- သင့် CV များတွင် ရေစာမပါဝင်ပါ"
        ),
    },
    "premium_priority_title": {
        "en": "⚡ *Priority Features*",
        "my": "⚡ *ဦးစားပေးလုပ်ဆောင်ချက်များ*",
    },
    "premium_priority": {
        "en": (
            "- Faster processing\n"
            "- Priority support\n"
            "- Regular template updates"
        ),
        "my": (
            "- မြန်ဆန်သောလုပ်ဆောင်မှု\n"
            "- ဦးစားပေးပံ့ပိုးကူညီမှု\n"
            "- Template အသစ်များပုံမှန်ထည့်သွင်းပေးခြင်း"
        ),
    },
    "premium_pricing_title": {
        "en": "💰 *Pricing Plans*",
        "my": "💰 *စျေးနှုန်းအစီအစဉ်များ*",
    },
    "premium_pricing": {
        "en": (
            "- 1 month: 19,000 MMK\n"
            "- 3 months: 50,000 MMK (15% off)\n"
            "- 1 year: 159,600 MMK (30% off)"
        ),
        "my": (
            "- ၁ လ: ၁၉,၀၀၀ ကျပ်\n"
            "- ၃ လ: ၅၀,၀၀၀ ကျပ် (၁၅% လျှော့စျေး)\n"
            "- ၁ နှစ်: ၁၅၉,၆၀၀ ကျပ် (၃၀% လျှော့စျေး)"
        ),
    },
    "premium_activation_instructions": {
        "en": (
            "🔑 To activate premium:\n"
            "1. Contact @ThantLwinMaung\n"
            "2. Get your premium key\n"
            "3. Use /redeem <key>"
        ),
        "my": (
            "🔑 Premium ကိုအသက်သွင်းရန်:\n"
            "၁. @ThantLwinMaung ကိုဆက်သွယ်ပါ\n"
            "၂. သင့် Premium သော့ကိုရယူပါ\n"
            "၃. /redeem <key> ကိုအသုံးပြုပါ"
        ),
    },
    "premium_preparing": {
        "en": "🔄 Preparing premium template examples...",
        "my": "🔄 Premium Template နမူနာများကိုပြင်ဆင်နေပါသည်...",
    },
    "premium_upgrade_reminder": {
        "en": (
            "🔓 *Upgrade to premium* to use these beautiful templates!\n\n"
            "Use /redeem with your premium key or contact @ThantLwinMaung to get started."
        ),
        "my": (
            "🔓 ဤလှပသော Template များကိုအသုံးပြုရန် *Premium သို့အဆင့်မြှင့်ပါ*!\n\n"
            "/redeem ဖြင့် သင့် Premium သော့ကိုထည့်သွင်းပါ သို့မဟုတ် @ThantLwinMaung ကိုဆက်သွယ်ပါ။"
        ),
    },

    # ===== GET PREMIUM =====
    "get_premium_title": {
        "en": "🌟 *Get Premium Access*",
        "my": "🌟 *Premium ရယူမည်*",
    },
    "get_premium_contact": {
        "en": "📩 Contact @ThantLwinMaung to get your premium key!",
        "my": "📩 သင့် Premium သော့ရယူရန် @ThantLwinMaung ကိုဆက်သွယ်ပါ!",
    },
    "get_premium_instructions": {
        "en": (
            "After receiving your premium key, use:\n"
            "`/redeem YOUR_KEY` to activate premium."
        ),
        "my": (
            "Premium သော့ရရှိပြီးနောက်၊ Premium ကိုအသက်သွင်းရန်:\n"
            "`/redeem YOUR_KEY` ကိုအသုံးပြုပါ။"
        ),
    },

    # ===== PRIVACY =====
    "privacy_title": {
        "en": "🔒 *Privacy Policy*",
        "my": "🔒 *ကိုယ်ရေးအချက်အလက်မူဝါဒ*",
    },
    "privacy_intro": {
        "en": "We take your privacy seriously. Please read our privacy policy at:",
        "my": "သင့်ရဲ့ကိုယ်ရေးအချက်အလက်ကို ကျွန်ုပ်တို့အလေးထားပါသည်။ ကျွန်ုပ်တို့၏ ကိုယ်ရေးအချက်အလက်မူဝါဒကို ဤနေရာတွင်ဖတ်ရှုပါ:",
    },
    "privacy_link": {
        "en": "[Privacy Policy Page]({url})",
        "my": "[ကိုယ်ရေးအချက်အလက်မူဝါဒ စာမျက်နှာ]({url})",
    },
    "privacy_key_points_title": {
        "en": "Key points:",
        "my": "အဓိကအချက်များ:",
    },
    "privacy_point_1": {
        "en": "- We don't store your personal data",
        "my": "- သင့်ကိုယ်ရေးအချက်အလက်များကိုကျွန်ုပ်တို့သိမ်းဆည်းမထားပါ",
    },
    "privacy_point_2": {
        "en": "- Your resume information is processed temporarily",
        "my": "- သင့် CV အချက်အလက်များကိုယာယီသာလုပ်ဆောင်ပါသည်",
    },
    "privacy_point_3": {
        "en": "- No data sharing with third parties",
        "my": "- တတိယပါတီများနှင့်ဒေတာမျှဝေခြင်းမရှိပါ",
    },

    # ===== LANGUAGE SETTINGS =====
    "language_title": {
        "en": "🌐 *Select Your Language*",
        "my": "🌐 *သင့်ဘာသာစကားကိုရွေးချယ်ပါ*",
    },
    "language_selected": {
        "en": "✅ Language set to *English* 🇬🇧",
        "my": "✅ ဘာသာစကားကို *မြန်မာ* 🇲🇲 သို့ပြောင်းလဲပြီးပါပြီ",
    },

    # ===== CANCEL =====
    "cancel_message": {
        "en": "🚫 Operation cancelled. Your progress has been cleared.",
        "my": "🚫 လုပ်ဆောင်ချက်ကိုဖျက်သိမ်းလိုက်ပါသည်။ သင့်တိုးတက်မှုများကိုရှင်းလင်းလိုက်ပါပြီ။",
    },

    # ===== ERRORS =====
    "error_prefix": {
        "en": "❌ Error processing your {field}. Please try again.",
        "my": "❌ သင့်ရဲ့ {field} ကိုလုပ်ဆောင်ရာတွင်အမှားရှိနေပါသည်။ ကျေးဇူးပြု၍ ပြန်လည်ကြိုးစားပါ။",
    },
    "error_generic": {
        "en": "❌ Failed to start resume creation. Please try again.",
        "my": "❌ CV ဖန်တီးခြင်းကိုစတင်ရန်မအောင်မြင်ပါ။ ကျေးဇူးပြု၍ ပြန်လည်ကြိုးစားပါ။",
    },
    "error_occurred": {
        "en": "❌ An error occurred. Our team has been notified.",
        "my": "❌ အမှားတစ်ခုဖြစ်ပွားခဲ့ပါသည်။ ကျွန်ုပ်တို့အဖွဲ့ကိုအကြောင်းကြားလိုက်ပါပြီ။",
    },
    "error_occurred_alert": {
        "en": "❌ Error occurred. Please try again.",
        "my": "❌ အမှားတစ်ခုဖြစ်ပွားခဲ့ပါသည်။ ကျေးဇူးပြု၍ ပြန်လည်ကြိုးစားပါ။",
    },

    # ===== REDEEM =====
    "redeem_usage": {
        "en": "Usage: `/redeem YOUR_KEY`\n\nContact @ThantLwinMaung to get a premium key.",
        "my": "အသုံးပြုနည်း: `/redeem YOUR_KEY`\n\nPremium သော့ရယူရန် @ThantLwinMaung ကိုဆက်သွယ်ပါ။",
    },
    "redeem_invalid_format": {
        "en": "❌ *Invalid Key Format*\n\nThe key you entered is not in the correct format.",
        "my": "❌ *သော့ဖော်မတ်မမှန်ပါ*\n\nသင်ထည့်သွင်းလိုက်သောသော့သည်ဖော်မတ်မှန်ကန်မှုမရှိပါ။",
    },
    "redeem_invalid_signature": {
        "en": "❌ *Invalid Key*\n\nThis key appears to be tampered with.",
        "my": "❌ *သော့မမှန်ကန်ပါ*\n\nဤသော့သည်ပြောင်းလဲခံထားရပုံပေါ်ပါသည်။",
    },
    "redeem_not_found": {
        "en": "❌ *Invalid Key*\n\nThis key was not found in our system.",
        "my": "❌ *သော့မမှန်ကန်ပါ*\n\nဤသော့ကိုကျွန်ုပ်တို့စနစ်တွင်ရှာမတွေ့ပါ။",
    },
    "redeem_expired": {
        "en": "❌ *Expired Key*\n\nThis key has already expired.",
        "my": "❌ *သော့သက်တမ်းကုန်သွားပါပြီ*\n\nဤသော့သည်သက်တမ်းကုန်ဆုံးသွားပါပြီ။",
    },
    "redeem_rate_limit": {
        "en": "⏳ Too many attempts! Please try again in {seconds} seconds.",
        "my": "⏳ ကြိုးစားမှုများလွန်းနေပါသည်! {seconds} စက္ကန့်အကြာတွင်ပြန်လည်ကြိုးစားပါ။",
    },
    "redeem_success": {
        "en": (
            "🎉 *Premium Activated!*\n\n"
            "Your premium access is valid until *{date}*.\n\n"
            "You now have access to all premium templates and features!"
        ),
        "my": (
            "🎉 *Premium အသက်သွင်းပြီးပါပြီ!*\n\n"
            "သင့် Premium အသုံးပြုခွင့်သည် *{date}* အထိသက်ဝင်ပါသည်။\n\n"
            "ယခုတွင် သင်သည် Premium Template များနှင့်လုပ်ဆောင်ချက်အားလုံးကိုသုံးခွင့်ရရှိပါပြီ!"
        ),
    },
    "redeem_error": {
        "en": "❌ Failed to redeem key. Please try again.",
        "my": "❌ သော့ကိုအသက်သွင်းရန်မအောင်မြင်ပါ။ ကျေးဇူးပြု၍ ပြန်လည်ကြိုးစားပါ။",
    },

    # ===== PREVIEW CAPTIONS =====
    "preview_caption": {
        "en": "Preview: {template}",
        "my": "နမူနာ: {template}",
    },
    "preview_error": {
        "en": "Couldn't generate {template} preview. Please try another template.",
        "my": "{template} နမူနာကိုဖန်တီးနိုင်ခြင်းမရှိပါ။ အခြား Template တစ်ခုကိုရွေးချယ်ပါ။",
    },
    "example_caption": {
        "en": "Example: {template}",
        "my": "ဥပမာ: {template}",
    },
    "example_error": {
        "en": "⚠️ Couldn't generate {template} example. Please try again later.",
        "my": "⚠️ {template} ဥပမာကိုဖန်တီးနိုင်ခြင်းမရှိပါ။ နောက်မှပြန်လည်ကြိုးစားပါ။",
    },

    # ===== PREMIUM GENERATE KEY =====
    "generate_key_success": {
        "en": (
            "🔑 *New Premium Key Generated*\n\n"
            "Key: `{key}`\n"
            "Duration: {duration} days\n"
            "Expires: {expiry}\n\n"
            "Share this with user:\n`/redeem {key}`"
        ),
        "my": (
            "🔑 *Premium သော့အသစ်ထုတ်ပေးပြီးပါပြီ*\n\n"
            "သော့: `{key}`\n"
            "ကြာချိန်: {duration} ရက်\n"
            "သက်တမ်းကုန်: {expiry}\n\n"
            "ဤသော့ကိုအသုံးပြုသူအားမျှဝေပါ:\n`/redeem {key}`"
        ),
    },
    "generate_key_error": {
        "en": "❌ Failed to generate key. Please check logs.",
        "my": "❌ သော့ထုတ်ပေးရန်မအောင်မြင်ပါ။ Log များကိုစစ်ဆေးပါ။",
    },
}
# ===== END LANGUAGE SUPPORT =====

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
        await update.message.reply_text("❌ You're not allowed to use this command.")
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
            await update.callback_query.answer("❌ You're not allowed to use this command.", show_alert=True)
        else:
            await update.message.reply_text("❌ You're not allowed to use this command.")
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
        BotCommand("language", "Change language / ဘာသာစကားပြောင်းရန်"),
        BotCommand("cancel", "Cancel current operation"),
    ]
    await application.bot.set_my_commands(commands)
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    asyncio.create_task(security_monitor(application))
    
    commands = await application.bot.get_my_commands()
    logger.info(f"Bot commands registered: {commands}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    lang = get_user_lang(user_id)

    keyboard = [
        [InlineKeyboardButton(t(user_id, "btn_new_resume"), callback_data="new_resume")],
        [InlineKeyboardButton(t(user_id, "btn_premium"), callback_data="premium_features")],
        [InlineKeyboardButton(t(user_id, "btn_help"), callback_data="show_help")],
        [InlineKeyboardButton(t(user_id, "btn_privacy"), callback_data="privacy_policy")],
        [InlineKeyboardButton(t(user_id, "btn_language"), callback_data="show_language")],
    ]

    greeting = t(user_id, "welcome", name=user.first_name)

    premium_status = (
        t(user_id, "premium_status_active")
        if is_premium(user.id)
        else t(user_id, "premium_status_inactive")
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
        "privacy_policy": show_privacy_policy,
        "show_language": show_language_menu,
    }

    # Language selection callbacks (lang_en, lang_my)
    if query.data.startswith("lang_"):
        lang_code = query.data.split("_")[1]
        user_id = query.from_user.id
        if set_user_lang(user_id, lang_code):
            await query.edit_message_text(
                t(user_id, "language_selected"),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="back_to_main")],
                ]),
            )
        return

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
            t(user_id, "resume_template_selected", template=TEMPLATES[template]), parse_mode="Markdown"
        )
        await generate_resume(update, context)


async def show_language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id

    keyboard = []
    for code, label in LANGUAGES.items():
        keyboard.append([InlineKeyboardButton(label, callback_data=f"lang_{code}")])

    keyboard.append([InlineKeyboardButton(t(user_id, "btn_back"), callback_data="back_to_main")])

    await query.edit_message_text(
        t(user_id, "language_title"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    keyboard = []
    for code, label in LANGUAGES.items():
        keyboard.append([InlineKeyboardButton(label, callback_data=f"lang_{code}")])

    keyboard.append([InlineKeyboardButton(t(user_id, "btn_back"), callback_data="back_to_main")])

    await update.message.reply_text(
        t(user_id, "language_title"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Help command triggered by {update.effective_user.id}")
    user_id = update.effective_user.id

    help_text = (
        f"{t(user_id, 'help_title')}\n\n"
        f"{t(user_id, 'help_getting_started_title')}\n"
        f"{t(user_id, 'help_getting_started')}\n\n"
        f"{t(user_id, 'help_premium_title')}\n"
        f"{t(user_id, 'help_premium')}\n\n"
        f"{t(user_id, 'help_activation_title')}\n"
        f"{t(user_id, 'help_activation')}\n\n"
        f"{t(user_id, 'help_commands_title')}\n"
        f"{t(user_id, 'help_commands')}\n\n"
        f"{t(user_id, 'help_contact')}"
    )
    keyboard = [[InlineKeyboardButton(t(user_id, "btn_back"), callback_data="back_to_main")]]
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
    user_id = update.effective_user.id
    privacy_policy_url = "https://privacyforresumegenie.onrender.com"  # Replace with your actual URL
    
    keyboard = [
        [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="back_to_main")],
    ]
    
    message = (
        f"{t(user_id, 'privacy_title')}\n\n"
        f"{t(user_id, 'privacy_intro')}\n"
        f"{t(user_id, 'privacy_link', url=privacy_policy_url)}\n\n"
        f"{t(user_id, 'privacy_key_points_title')}\n"
        f"{t(user_id, 'privacy_point_1')}\n"
        f"{t(user_id, 'privacy_point_2')}\n"
        f"{t(user_id, 'privacy_point_3')}"
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
            f"{t(user_id, 'resume_start_title')}\n\n"
            f"{t(user_id, 'resume_start_desc')}\n\n"
            f"{t(user_id, 'resume_step_name')}"
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
        user_id = update.effective_user.id
        error_msg = t(user_id, "error_generic")
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
            t(user_id, "resume_step_contact"),
            parse_mode="Markdown",
        )
        return CONTACT
    except Exception as e:
        logger.error(f"Error in get_name: {e}")
        user_id = update.effective_user.id
        await update.message.reply_text(t(user_id, "error_prefix", field="name"))
        return ConversationHandler.END


async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["contact"] = update.message.text

    await update.message.reply_text(
        t(user_id, "resume_step_education"),
        parse_mode="Markdown",
    )
    return EDUCATION


async def get_education(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["education"] = update.message.text

    await update.message.reply_text(
        t(user_id, "resume_step_experience"),
        parse_mode="Markdown",
    )
    return EXPERIENCE


async def get_experience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["experience"] = update.message.text

    await update.message.reply_text(
        t(user_id, "resume_step_skills"),
        parse_mode="Markdown",
    )
    return SKILLS


async def get_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id]["skills"] = update.message.text

    await update.message.reply_text(
        t(user_id, "resume_step_summary"),
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
                    caption=t(user_id, "preview_caption", template=TEMPLATES[template]),
                )
            except Exception as e:
                print(f"Error generating {template} preview: {e}")
                await update.message.reply_text(
                    t(user_id, "preview_error", template=template)
                )

        await update.message.reply_text(
            t(user_id, "resume_template_choose"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END
    else:
        user_data[user_id]["template"] = "BASIC"
        await update.message.reply_text(
            t(user_id, "resume_generating", template="Basic"),
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
        await message.reply_text(t(user_id, "resume_error_data"))
        return ConversationHandler.END

    # Generate PDF
    try:
        pdf_bytes = generate_pdf_bytes(user_data[user_id])
    except Exception as e:
        await message.reply_text(t(user_id, "resume_error_generate"))
        return ConversationHandler.END

    # Send to user
    await message.reply_document(
        document=io.BytesIO(pdf_bytes),
        filename=f"{user_data[user_id]['name']}_Resume.pdf",
        caption=t(user_id, "resume_ready"),
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
    user_id = update.effective_user.id

    premium_text = (
        f"{t(user_id, 'premium_title')}\n\n"
        f"{t(user_id, 'premium_templates_title')}\n"
        f"{t(user_id, 'premium_templates')}\n\n"
        f"{t(user_id, 'premium_unlimited_title')}\n"
        f"{t(user_id, 'premium_unlimited')}\n\n"
        f"{t(user_id, 'premium_priority_title')}\n"
        f"{t(user_id, 'premium_priority')}\n\n"
        f"{t(user_id, 'premium_pricing_title')}\n"
        f"{t(user_id, 'premium_pricing')}\n\n"
        f"{t(user_id, 'premium_activation_instructions')}"
    )
    keyboard = [
        [InlineKeyboardButton(t(user_id, "btn_get_premium"), callback_data="get_premium")],
        [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="back_to_main")],
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
        text=t(user_id, "premium_preparing"),
        parse_mode="Markdown",
    )

    # Generate and send previews of premium templates
    for template in ["MODERN", "CREATIVE", "MINIMALIST"]:
        try:
            pdf_bytes = generate_pdf_bytes({**example_data, "template": template}, preview_mode=True)
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=io.BytesIO(pdf_bytes),
                filename=f"{template}_example.pdf",
                caption=t(user_id, "example_caption", template=TEMPLATES[template]),
                parse_mode="Markdown",
            )

        except Exception as e:
            print(f"Error generating {template} example: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=t(user_id, "example_error", template=template),
                parse_mode="Markdown",
            )

    # Add reminder for non-premium users
    if not is_premium(update.effective_user.id):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=t(user_id, "premium_upgrade_reminder"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def get_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id

    keyboard = [
        [InlineKeyboardButton(t(user_id, "btn_premium"), callback_data="premium_features")],
        [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="back_to_main")],
    ]

    await query.edit_message_text(
        f"{t(user_id, 'get_premium_title')}\n\n{t(user_id, 'get_premium_contact')}\n\n"
        f"{t(user_id, 'get_premium_instructions')}",
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
            t(update.effective_user.id, "generate_key_success", key=key, duration=duration, expiry=expiry),
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"GenerateKey Error: {e}")
        await update.message.reply_text(t(update.effective_user.id, "generate_key_error"), parse_mode="Markdown")

async def redeem_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if check_rate_limit(user_id):
        remaining_time = int(REDEEM_COOLDOWN - (time.time() - redeem_attempts[user_id]['last_attempt']))
        await update.message.reply_text(
            t(update.effective_user.id, "redeem_rate_limit", seconds=remaining_time),
            parse_mode="Markdown",
        )
        return

    if not context.args or len(context.args) != 1:
        record_attempt(user_id, False)
        await update.message.reply_text(
            t(update.effective_user.id, "redeem_usage"),
            parse_mode="Markdown",
        )
        return

    input_key = context.args[0].strip()
    if not validate_key_format(input_key):
        record_attempt(user_id, False)
        log_security_event("invalid_key_format", user_id, input_key)
        await update.message.reply_text(
            t(update.effective_user.id, "redeem_invalid_format"),
            parse_mode="Markdown",
        )
        return

    if not verify_key_signature(input_key):
        record_attempt(user_id, False)
        log_security_event("invalid_key_signature", user_id, input_key)
        await update.message.reply_text(
            t(update.effective_user.id, "redeem_invalid_signature"),
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
                    t(update.effective_user.id, "redeem_not_found"),
                    parse_mode="Markdown",
                )
                return

            expiry_date = row[0]
            if expiry_date < datetime.now().date():
                record_attempt(user_id, False)
                log_security_event("expired_key", user_id, input_key)
                await update.message.reply_text(
                    t(update.effective_user.id, "redeem_expired"),
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
            t(update.effective_user.id, "redeem_success", date=expiry_date),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(update.effective_user.id, "btn_premium"), callback_data="premium_features")],
                [InlineKeyboardButton(t(update.effective_user.id, "btn_new_resume"), callback_data="new_resume")],
            ]),
        )

    except Exception as e:
        logger.error(f"RedeemKey Error: {e}")
        await update.message.reply_text(t(update.effective_user.id, "redeem_error"), parse_mode="Markdown")

        
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_data:
        del user_data[user_id]

    await update.message.reply_text(
        t(user_id, "cancel_message"),
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update else None
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
        await update.message.reply_text(t(user_id, "error_occurred"))
    elif update and update.callback_query:
        await update.callback_query.answer(t(user_id, "error_occurred_alert"), show_alert=True)

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
    app.add_handler(CommandHandler("language", language_command))
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
    
    # Start Telegram bot
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
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
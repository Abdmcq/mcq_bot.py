import logging
import os
import tempfile
import re
import requests
import json
import asyncio
from datetime import datetime

from telegram import Update, ReplyKeyboardRemove, Poll
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from PyPDF2 import PdfReader
from flask import Flask, request

# إعدادات التسجيل (Logging)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# --- متغيرات البيئة (يجب تعيينها في Render) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

# --- معلومات البوت ---
OWNER_USERNAME = "ll7ddd"
BOT_PROGRAMMER_NAME = "عبدالرحمن حسن"

# حالات المحادثة
ASK_NUM_QUESTIONS_FOR_EXTRACTION = range(1)

# --- دوال مساعدة ---
def extract_text_from_pdf(pdf_path: str) -> str:
    try:
        reader = PdfReader(pdf_path)
        text = "".join(page.extract_text() + "\n" for page in reader.pages if page.extract_text())
        return text
    except Exception as e:
        logger.error(f"خطأ في استخراج نص PDF: {e}")
        return ""

def generate_mcqs_text_blob_with_gemini(text_content: str, num_questions: int, language: str = "Arabic") -> str:
    if not GEMINI_API_KEY:
        logger.error("مفتاح Gemini API غير موجود.")
        return ""

    api_model = "gemini-1.5-flash-latest"
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{api_model}:generateContent?key={GEMINI_API_KEY}"
    max_chars = 20000
    text_content = text_content[:max_chars] if len(text_content) > max_chars else text_content

    prompt = f"""
    Generate exactly {num_questions} MCQs in {language} from the text below.
    The questions should aim to comprehensively cover the key information and concepts from the entire provided text.

    STRICT FORMAT (EACH PART ON A NEW LINE):
    Question: [Question text, can be multi-line ending with ? or not]
    A) [Option A text]
    B) [Option B text]
    C) [Option C text]
    D) [Option D text]
    Correct Answer: [Correct option letter, e.g., A, B, C, or D]
    --- (Separator, USED BETWEEN EACH MCQ, BUT NOT after the last MCQ)

    Text:
    \"\"\"
    {text_content}
    \"\"\"
    CRITICAL INSTRUCTIONS:
    1. Each question MUST have exactly 4 options (A, B, C, D). Do not generate questions with fewer than 4 options.
    2. Ensure question text is 10-290 characters long.
    3. Ensure each option text (A, B, C, D) is 1-90 characters long.
    4. The "Correct Answer:" line is CRITICAL and must be present for every MCQ.
    5. The "Correct Answer:" must be one of A, B, C, or D, corresponding to one of the provided options.
    6. Distractor options (incorrect answers) should be plausible but clearly incorrect based on the text.
    """

    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192}}
    headers = {'Content-Type': 'application/json'}
    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=300)
        response.raise_for_status()
        generated_text_candidate = response.json().get("candidates")
        if generated_text_candidate and len(generated_text_candidate) > 0:
            content_parts = generated_text_candidate[0].get("content", {}).get("parts")
            if content_parts and len(content_parts) > 0:
                generated_text = content_parts[0].get("text", "")
                logger.debug(f"استجابة Gemini الخام (أول 500 حرف): {generated_text[:500]}")
                return generated_text.strip()
        logger.error(f"استجابة Gemini API تفتقر إلى الهيكل المتوقع. الاستجابة: {response.json()}")
        return ""
    except requests.exceptions.Timeout:
        logger.error(f"انتهت مهلة طلب Gemini API بعد 300 ثانية لـ {num_questions} سؤال.")
        return ""
    except Exception as e:
        logger.error(f"خطأ في Gemini API: {e}", exc_info=True)
        if hasattr(e, 'response') and e.response is not None: logger.error(f"استجابة Gemini: {e.response.text}")
        return ""

mcq_parsing_pattern = re.compile(
    r"Question:\s*(.*?)\s*\n"
    r"A\)\s*(.*?)\s*\n"
    r"B\)\s*(.*?)\s*\n"
    r"C\)\s*(.*?)\s*\n"
    r"D\)\s*(.*?)\s*\n"
    r"Correct Answer:\s*([A-D])",
    re.IGNORECASE | re.DOTALL
)

async def send_single_mcq_as_poll(mcq_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    match = mcq_parsing_pattern.fullmatch(mcq_text.strip())
    if not match:
        logger.warning(f"تعذر تحليل كتلة MCQ للاستطلاع (عدم تطابق التنسيق أو ليست 4 خيارات):\n-----\n{mcq_text}\n-----")
        return False
    try:
        question_text = match.group(1).strip()
        option_a_text = match.group(2).strip()
        option_b_text = match.group(3).strip()
        option_c_text = match.group(4).strip()
        option_d_text = match.group(5).strip()
        correct_answer_letter = match.group(6).upper()

        options = [option_a_text, option_b_text, option_c_text, option_d_text]

        if not (1 <= len(question_text) <= 300):
            logger.warning(f"نص سؤال الاستطلاع طويل/قصير جداً ({len(question_text)} حرف): \"{question_text[:50]}...\"")
            return False
        valid_options_for_poll = True
        for i, opt_text in enumerate(options):
            if not (1 <= len(opt_text) <= 100):
                logger.warning(f"نص خيار الاستطلاع {i+1} طويل/قصير جداً ({len(opt_text)} حرف): \"{opt_text[:50]}...\" للسؤال \"{question_text[:50]}...\"")
                valid_options_for_poll = False
                break
        if not valid_options_for_poll: return False

        correct_option_id = -1
        letter_to_id = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        if correct_answer_letter in letter_to_id:
            correct_option_id = letter_to_id[correct_answer_letter]

        if correct_option_id == -1:
            logger.error(f"حرف الإجابة الصحيح غير صالح '{correct_answer_letter}'. MCQ:\n{mcq_text}")
            return False

        await context.bot.send_poll(
            chat_id=update.effective_chat.id,
            question=question_text,
            options=options,
            type=Poll.QUIZ,
            correct_option_id=correct_option_id,
            is_anonymous=True,
        )
        return True
    except Exception as e:
        logger.error(f"خطأ في إنشاء استطلاع من كتلة MCQ: {e}\nMCQ:\n{mcq_text}", exc_info=True)
        return False

# --- منطق تقييد الوصول (بدون حفظ بيانات المستخدمين) ---
async def handle_restricted_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        logger.warning("handle_restricted_access called without effective_user.")
        return

    logger.info(f"User {user.id} ({user.username or user.first_name}) attempted restricted access.")

    await update.message.reply_text(
        f"عذراً، هذا البوت يعمل بشكل حصري لمبرمجه {BOT_PROGRAMMER_NAME} (@{OWNER_USERNAME}).\n"
        "لا يمكنك استخدام وظائفه حالياً."
    )

# --- معالجات الأوامر والمحادثات ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        logger.warning("Start command received without effective_user.")
        return

    logger.info(f"Received /start command from user ID: {user.id} (OWNER_ID is: {OWNER_ID})")

    if user.id != OWNER_ID:
        logger.warning(f"User {user.id} is not the owner ({OWNER_ID}). Restricting access for /start.")
        await handle_restricted_access(update, context)
        return

    await update.message.reply_html(
        rf"مرحباً {update.effective_user.mention_html()}!\n"
        rf"أرسل ملف PDF لاستخراج أسئلة منه. الأسئلة ستُحول إلى اختبارات (quiz polls) مع 4 خيارات لكل سؤال."
    )

async def handle_pdf_for_extraction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user:
        logger.warning("PDF extraction received without effective_user.")
        return ConversationHandler.END

    logger.info(f"Received PDF for extraction from user ID: {user.id} (OWNER_ID is: {OWNER_ID})")

    if user.id != OWNER_ID:
        logger.warning(f"User {user.id} is not the owner ({OWNER_ID}). Restricting access for PDF upload.")
        await handle_restricted_access(update, context)
        return ConversationHandler.END

    document = update.message.document
    if not document or not document.mime_type == "application/pdf":
        await update.message.reply_text("من فضلك أرسل ملف PDF صالح.")
        return ConversationHandler.END

    await update.message.reply_text("تم استلام ملف PDF. جاري معالجة النص...")
    try:
        pdf_file = await document.get_file()
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = os.path.join(temp_dir, document.file_name or "temp.pdf")
            await pdf_file.download_to_drive(custom_path=pdf_path)
            text_content = extract_text_from_pdf(pdf_path)
    except Exception as e:
        logger.error(f"خطأ في التعامل مع المستند: {e}", exc_info=True)
        await update.message.reply_text("حدث خطأ أثناء معالجة الملف.")
        return ConversationHandler.END

    if not text_content.strip():
        await update.message.reply_text("لم أتمكن من استخراج أي نص من ملف PDF.")
        return ConversationHandler.END

    context.user_data['pdf_text_for_extraction'] = text_content
    await update.message.reply_text("النص استخرج. كم سؤال (quiz poll) بأربعة خيارات تريد؟ مثال: 5. يمكنك طلب أي عدد (مثلاً 50).")
    return ASK_NUM_QUESTIONS_FOR_EXTRACTION

async def num_questions_for_extraction_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user:
        logger.warning("Num questions received without effective_user.")
        return ConversationHandler.END

    logger.info(f"Received number of questions from user ID: {user.id} (OWNER_ID is: {OWNER_ID})")

    if user.id != OWNER_ID:
        logger.warning(f"User {user.id} is not the owner ({OWNER_ID}). Restricting access for num questions.")
        await handle_restricted_access(update, context)
        return ConversationHandler.END

    try:
        num_questions_str = update.message.text
        if not num_questions_str.isdigit():
            await update.message.reply_text("الرجاء إرسال رقم صحيح موجب لعدد الأسئلة.")
            return ASK_NUM_QUESTIONS_FOR_EXTRACTION

        num_questions = int(num_questions_str)

        if num_questions < 1:
            await update.message.reply_text("الرجاء إدخال رقم موجب (1 أو أكثر) لعدد الأسئلة.")
            return ASK_NUM_QUESTIONS_FOR_EXTRACTION

        if num_questions > 50:
            await update.message.reply_text(
                f"لقد طلبت إنشاء {num_questions} اختباراً (4 خيارات لكل سؤال). "
                "قد تستغرق هذه العملية بعض الوقت. سأبذل قصارى جهدي!"
            )
        elif num_questions > 20:
             await update.message.reply_text(
                f"جاري تجهيز {num_questions} اختباراً (4 خيارات لكل سؤال). قد يستغرق هذا بضع لحظات..."
            )

    except ValueError:
        await update.message.reply_text("الرجاء إرسال رقم صحيح موجب لعدد الأسئلة.")
        return ConversationHandler.END

    pdf_text = context.user_data.pop('pdf_text_for_extraction', None)
    if not pdf_text:
        await update.message.reply_text("خطأ: نص PDF غير موجود. أعد إرسال الملف.")
        return ConversationHandler.END

    await update.message.reply_text(f"جاري استخراج {num_questions} سؤالاً (4 خيارات لكل سؤال) وتحويلها إلى اختبارات...", reply_markup=ReplyKeyboardRemove())

    generated_mcq_text_blob = generate_mcqs_text_blob_with_gemini(pdf_text, num_questions)

    if not generated_mcq_text_blob:
        await update.message.reply_text("لم أتمكن من استخراج أسئلة من النموذج باستخدام Gemini API.")
        return ConversationHandler.END

    individual_mcqs_texts = [
        mcq.strip() for mcq in re.split(r'\s*---\s*', generated_mcq_text_blob)
        if mcq.strip() and "Correct Answer:" in mcq and "D)" in mcq
    ]

    if not individual_mcqs_texts:
        await update.message.reply_text("لم يتمكن Gemini من إنشاء أسئلة بالتنسيق المطلوب (4 خيارات) أو النص المستخرج فارغ.")
        logger.warning(f"Gemini blob did not yield valid 4-option MCQs: {generated_mcq_text_blob[:300]}")
        return ConversationHandler.END

    actual_generated_count = len(individual_mcqs_texts)
    if actual_generated_count < num_questions:
        await update.message.reply_text(
            f"تم طلب {num_questions} اختباراً، ولكن تمكنت من إنشاء {actual_generated_count} اختباراً فقط بالتنسيق المطلوب (4 خيارات). "
            "قد يكون هذا بسبب طبيعة النص المدخل أو استجابة Gemini."
        )

    await update.message.reply_text(f"جاري الآن إنشاء {actual_generated_count} اختباراً (quiz polls)...")

    polls_created_count = 0
    delay_between_polls = 0.25

    for mcq_text_item in individual_mcqs_texts:
        if await send_single_mcq_as_poll(mcq_text_item, update, context):
            polls_created_count += 1

        if actual_generated_count > 10:
            await asyncio.sleep(delay_between_polls)

    final_message = f"انتهت العملية.\n"
    final_message += f"تم إنشاء {polls_created_count} اختبار (quiz poll) بنجاح (من أصل {actual_generated_count} سؤال تم إنشاؤه بواسطة Gemini بالتنسيق المطلوب)."
    if polls_created_count < actual_generated_count:
        final_message += f"\nتعذر إنشاء {actual_generated_count - polls_created_count} اختبار بسبب مشاكل في التنسيق لم يتم التعرف عليها أو حدود تيليجرام."

    await update.message.reply_text(final_message)

    return ConversationHandler.END

async def cancel_extraction_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user:
        logger.warning("Cancel command received without effective_user.")
        return ConversationHandler.END

    logger.info(f"Received /cancel command from user ID: {user.id} (OWNER_ID is: {OWNER_ID})")

    if user.id != OWNER_ID:
        logger.warning(f"User {user.id} is not the owner ({OWNER_ID}). Restricting access for /cancel.")
        await handle_restricted_access(update, context)
        return ConversationHandler.END

    await update.message.reply_text("تم إلغاء العملية.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"حدث خطأ {context.error} في التحديث {update}", exc_info=True)
    if update and update.effective_message:
        if isinstance(context.error, TelegramError) and "message to edit not found" in str(context.error).lower():
            return
        try:
            if update.effective_user and update.effective_user.id == OWNER_ID:
                 await update.effective_message.reply_text(f"عذراً، حدث خطأ ما: {context.error}")
            else:
                 await update.effective_message.reply_text("عذراً، حدث خطأ ما داخلياً.")
        except Exception as e_reply:
            logger.error(f"خطأ في إرسال رسالة الخطأ: {e_reply}")

app = Flask(__name__)
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

extraction_conv_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.Document.PDF, handle_pdf_for_extraction)],
    states={
        ASK_NUM_QUESTIONS_FOR_EXTRACTION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, num_questions_for_extraction_received)
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_extraction_command)],
    conversation_timeout=1200
)
application.add_handler(CommandHandler("start", start_command))
application.add_handler(extraction_conv_handler)
application.add_error_handler(error_handler)

@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def webhook_handler():
    # هذا السطر سيسجل أي طلب POST يصل إلى هذا المسار
    logger.debug(f"Received POST request on webhook path. Headers: {request.headers}")
    logger.debug(f"Request JSON data: {request.get_json(silent=True)}")

    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        await application.process_update(update)
    return "ok"

@app.route("/")
def index():
    return "Bot is running!"

async def setup_webhook():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN غير معرّف. لا يمكن إعداد الويب هوك.")
        return

    webhook_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not webhook_host:
        logger.error("RENDER_EXTERNAL_HOSTNAME غير معرّف. الويب هوك لن يتم إعداده بشكل صحيح.")
        return

    full_webhook_url = f"https://{webhook_host}/{TELEGRAM_BOT_TOKEN}"
    logger.info(f"جاري إعداد الويب هوك إلى: {full_webhook_url}")

    try:
        await application.bot.set_webhook(url=full_webhook_url)
        logger.info("تم إعداد الويب هوك بنجاح.")
    except TelegramError as e:
        logger.error(f"فشل إعداد الويب هوك: {e}")

if __name__ == "__main__":
    if OWNER_ID == 0:
        logger.warning("OWNER_ID غير معرّف أو مُعيّن على القيمة الافتراضية (0). الرجاء تعيينه في متغيرات بيئة Render.")
        print("\n" + "="*50)
        print("هام: الرجاء تعيين 'OWNER_ID' في متغيرات بيئة Render إلى معرف مستخدم تليجرام الرقمي الخاص بك.")
        print("="*50 + "\n")
    pass


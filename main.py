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
from flask import Flask, request # استيراد Flask و request للويب هوك

# إعدادات التسجيل (Logging)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- متغيرات البيئة (يجب تعيينها في Render) ---
# ستقوم Render بتوفير هذه المتغيرات
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# تأكد من تحويل OWNER_ID إلى عدد صحيح
OWNER_ID = int(os.environ.get("OWNER_ID", "0")) # قيمة افتراضية 0 لتجنب الخطأ إذا لم يتم تعيينها

# --- معلومات البوت ---
OWNER_USERNAME = "ll7ddd" # يمكنك تغيير هذا إذا كنت تريد
BOT_PROGRAMMER_NAME = "عبدالرحمن حسن" # يمكنك تغيير هذا إذا كنت تريد

# حالات المحادثة
ASK_NUM_QUESTIONS_FOR_EXTRACTION = range(1)

# --- دوال مساعدة ---
def extract_text_from_pdf(pdf_path: str) -> str:
    """
    يستخرج النص من ملف PDF محدد.
    """
    try:
        reader = PdfReader(pdf_path)
        text = "".join(page.extract_text() + "\n" for page in reader.pages if page.extract_text())
        return text
    except Exception as e:
        logger.error(f"خطأ في استخراج نص PDF: {e}")
        return ""

def generate_mcqs_text_blob_with_gemini(text_content: str, num_questions: int, language: str = "Arabic") -> str:
    """
    يولد أسئلة اختيار من متعدد (MCQs) باستخدام Gemini API.
    """
    if not GEMINI_API_KEY:
        logger.error("مفتاح Gemini API غير موجود.")
        return ""

    api_model = "gemini-1.5-flash-latest" # يمكنك تجربة نماذج أخرى إذا أردت
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{api_model}:generateContent?key={GEMINI_API_KEY}"
    max_chars = 20000 # الحد الأقصى للأحرف التي يمكن إرسالها إلى Gemini

    # قص النص إذا كان طويلاً جداً
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
        response.raise_for_status() # يرفع استثناء لأكواد حالة HTTP 4xx/5xx
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

# نمط تحليل أسئلة الاختيار من متعدد
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
    """
    يرسل سؤال اختيار من متعدد واحد كاستطلاع (poll) في تليجرام.
    """
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

        # التحقق من طول السؤال والخيارات لمتطلبات تليجرام
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
    """
    يرسل رسالة وصول مقيد للمستخدمين غير المصرح لهم.
    لا يقوم بتخزين أي بيانات عن المستخدمين.
    """
    user = update.effective_user
    if not user:
        return

    await update.message.reply_text(
        f"عذراً، هذا البوت يعمل بشكل حصري لمبرمجه {BOT_PROGRAMMER_NAME} (@{OWNER_USERNAME}).\n"
        "لا يمكنك استخدام وظائفه حالياً."
    )

# --- معالجات الأوامر والمحادثات ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    يستجيب لأمر /start.
    """
    user = update.effective_user
    if user.id != OWNER_ID:
        await handle_restricted_access(update, context)
        return

    await update.message.reply_html(
        rf"مرحباً {update.effective_user.mention_html()}!\n"
        rf"أرسل ملف PDF لاستخراج أسئلة منه. الأسئلة ستُحول إلى اختبارات (quiz polls) مع 4 خيارات لكل سؤال."
    )

async def handle_pdf_for_extraction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    يتعامل مع ملفات PDF المرسلة لاستخراج النص.
    """
    user = update.effective_user
    if user.id != OWNER_ID:
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
    """
    يتعامل مع عدد الأسئلة المطلوبة من المستخدم.
    """
    user = update.effective_user
    if user.id != OWNER_ID:
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
        return ASK_NUM_QUESTIONS_FOR_EXTRACTION

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

    # لا يوجد حفظ لملف MCQs هنا، سيتم إرسالها مباشرة كـ polls
    await update.message.reply_text(f"جاري الآن إنشاء {actual_generated_count} اختباراً (quiz polls)...")

    polls_created_count = 0
    delay_between_polls = 0.25 # لتجنب تجاوز حدود تليجرام

    for mcq_text_item in individual_mcqs_texts:
        if await send_single_mcq_as_poll(mcq_text_item, update, context):
            polls_created_count += 1
        # تأخير بسيط بين إرسال الاستطلاعات لتجنب تجاوز حدود معدل تليجرام
        if actual_generated_count > 10: # فقط إذا كان هناك عدد كبير من الأسئلة
            await asyncio.sleep(delay_between_polls)

    final_message = f"انتهت العملية.\n"
    final_message += f"تم إنشاء {polls_created_count} اختبار (quiz poll) بنجاح (من أصل {actual_generated_count} سؤال تم إنشاؤه بواسطة Gemini بالتنسيق المطلوب)."
    if polls_created_count < actual_generated_count:
        final_message += f"\nتعذر إنشاء {actual_generated_count - polls_created_count} اختبار بسبب مشاكل في التنسيق لم يتم التعرف عليها أو حدود تيليجرام."

    await update.message.reply_text(final_message)

    return ConversationHandler.END

async def cancel_extraction_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    يلغي عملية استخراج الأسئلة.
    """
    user = update.effective_user
    if user.id != OWNER_ID:
        await handle_restricted_access(update, context)
        return ConversationHandler.END

    await update.message.reply_text("تم إلغاء العملية.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear() # مسح بيانات المستخدم للمحادثة الحالية
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يتعامل مع الأخطاء التي تحدث في البوت.
    """
    logger.error(f"حدث خطأ {context.error} في التحديث {update}", exc_info=True)
    if update and update.effective_message:
        # تجنب إرسال رسائل خطأ لبعض الأخطاء الشائعة التي لا تتطلب تدخل المستخدم
        if isinstance(context.error, TelegramError) and "message to edit not found" in str(context.error).lower():
            return
        try:
            # إرسال رسالة خطأ للمالك فقط
            if update.effective_user and update.effective_user.id == OWNER_ID:
                 await update.effective_message.reply_text(f"عذراً، حدث خطأ ما: {context.error}")
            else:
                # للمستخدمين غير المالكين، رسالة عامة
                 await update.effective_message.reply_text("عذراً، حدث خطأ ما داخلياً.")
        except Exception as e_reply:
            logger.error(f"خطأ في إرسال رسالة الخطأ: {e_reply}")

# تهيئة تطبيق Flask
app = Flask(__name__)

# تهيئة تطبيق البوت
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build() # لا يوجد persistence

# إضافة المعالجات
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

# نقطة نهاية الـ webhook
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def webhook_handler():
    """
    يتعامل مع تحديثات تليجرام الواردة عبر الويب هوك.
    """
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        await application.process_update(update)
    return "ok"

# نقطة نهاية للتحقق من أن الخادم يعمل (Health Check)
@app.route("/")
def index():
    """
    نقطة نهاية بسيطة للتحقق من أن التطبيق يعمل.
    """
    return "Bot is running!"

async def setup_webhook():
    """
    يقوم بإعداد الويب هوك على جانب تليجرام.
    يجب استدعاء هذه الدالة مرة واحدة عند بدء تشغيل التطبيق.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN غير معرّف. لا يمكن إعداد الويب هوك.")
        return

    # Render يوفر متغير البيئة RENDER_EXTERNAL_HOSTNAME
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
    # تحقق من OWNER_ID عند بدء التشغيل
    if OWNER_ID == 0: # قيمة افتراضية لتجنب الخطأ
        logger.warning("OWNER_ID غير معرّف أو مُعيّن على القيمة الافتراضية (0). الرجاء تعيينه في متغيرات بيئة Render.")
        print("\n" + "="*50)
        print("هام: الرجاء تعيين 'OWNER_ID' في متغيرات بيئة Render إلى معرف مستخدم تليجرام الرقمي الخاص بك.")
        print("="*50 + "\n")

    # تشغيل إعداد الويب هوك عند بدء تشغيل التطبيق
    # يتم تشغيل تطبيق Flask بواسطة Gunicorn على Render، لذا لا نستخدم app.run() هنا
    # يجب أن يتم استدعاء setup_webhook() مرة واحدة عند بدء تشغيل الحاوية.
    # أفضل طريقة للقيام بذلك هي من خلال أمر البدء في Procfile
    # أو التأكد من أن Flask app يستدعيها عند تهيئته.
    # في هذا الإعداد، سيتم استدعاء setup_webhook() عند بدء تشغيل Gunicorn.
    # يمكننا تشغيلها بشكل مستقل هنا للاختبار المحلي إذا أردنا.
    # For Render, the Gunicorn command will run the Flask app.
    # The webhook setup should be part of the application's startup logic.
    # A common pattern is to have a separate script for setup or call it
    # from within the Flask app's startup.
    # For simplicity, we'll assume Gunicorn runs the Flask app, and the
    # webhook setup happens as part of the app's initialization or a pre-start hook.
    # For local testing, you might run:
    # asyncio.run(setup_webhook())
    # app.run(port=int(os.environ.get("PORT", 5000)))
    pass # Gunicorn will run the Flask app


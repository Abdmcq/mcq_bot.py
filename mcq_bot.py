import logging
import os
import asyncio
from telegram import Update, Document, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler # <-- إضافة CallbackQueryHandler
)
from PyPDF2 import PdfReader
import google.generativeai as genai
import time

# --- أدخل التوكنات الخاصة بك هنا مباشرة ---
TELEGRAM_BOT_TOKEN = "6608888663:AAGMZrD-c328tqXCZYEkKBjGUfCsqmPlJrk"  # <-- ضع توكن تليجرام هنا
GOOGLE_API_KEY = "AIzaSyAB24hOiaVwfOjDl36RbpMetlBqW1a7jDs"      # <-- ضع مفتاح Google API هنا
# -----------------------------------------

# --- إعداد Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
# ---------------------------

# التحقق من وجود التوكنات
if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_ACTUAL_TELEGRAM_BOT_TOKEN":
    print("ERROR: Please replace 'YOUR_ACTUAL_TELEGRAM_BOT_TOKEN' with your actual Telegram Bot Token in the code.")
    raise ValueError("Telegram Bot Token not set correctly.")
if not GOOGLE_API_KEY or GOOGLE_API_KEY == "YOUR_ACTUAL_GOOGLE_API_KEY":
     print("ERROR: Please replace 'YOUR_ACTUAL_GOOGLE_API_KEY' with your actual Google API Key in the code.")
     raise ValueError("Google API Key not set correctly.")


# إعداد Google Generative AI SDK
try:
    genai.configure(api_key=GOOGLE_API_KEY)
    logger.info("Google Generative AI SDK configured successfully.")
    GEMINI_MODEL = 'gemini-1.5-flash-latest'
    model = genai.GenerativeModel(GEMINI_MODEL)
    logger.info(f"Using Gemini model: {GEMINI_MODEL}")
except Exception as e:
    logger.error(f"Failed to configure Google Generative AI SDK: {e}")
    model = None

# --- تعريف بيانات الأزرار (Callback Data) ---
CALLBACK_GENERATE_5 = "generate_5"
CALLBACK_GENERATE_10 = "generate_10"
CALLBACK_GENERATE_SPECIFY = "generate_specify"
CALLBACK_CLEAR = "clear_pdfs"
CALLBACK_UPLOAD_INFO = "upload_info" # زر لإظهار معلومات الرفع

# --- دالات مساعدة للمنطق الأساسي ---

async def _perform_clear(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """المنطق الأساسي لمسح ملفات PDF."""
    if chat_id in context.user_data and 'pdfs' in context.user_data[chat_id] and context.user_data[chat_id]['pdfs']:
        count = len(context.user_data[chat_id]['pdfs'])
        context.user_data[chat_id]['pdfs'] = []
        logger.info(f"Cleared {count} PDFs for chat_id {chat_id}")
        return f"🗑️ تم مسح {count} محاضرة/محاضرات مرفوعة بنجاح."
    else:
        logger.info(f"No PDFs to clear for chat_id {chat_id}")
        return "⚠️ لا توجد محاضرات مرفوعة لمسحها."

async def _perform_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, num_questions_args: list[str]) -> None:
    """المنطق الأساسي لإنشاء الأسئلة."""
    chat_id = update.effective_chat.id
    message = update.effective_message # رسالة الأمر أو رسالة الزر

    if not model:
         await message.reply_text("عذراً، هناك مشكلة في تهيئة نموذج Google AI. لا يمكن إنشاء الأسئلة حالياً.")
         return

    if chat_id not in context.user_data or not context.user_data[chat_id].get('pdfs'):
        await message.reply_text("⚠️ لم يتم رفع أي ملفات PDF. يرجى رفع محاضرة واحدة على الأقل أولاً.")
        return

    num_pdfs_uploaded = len(context.user_data[chat_id]['pdfs'])
    num_questions_per_pdf = []

    try:
        if len(num_questions_args) == 1:
            num_q = int(num_questions_args[0])
            if num_q <= 0 or num_q > 20:
                await message.reply_text("⚠️ عدد الأسئلة يجب أن يكون بين 1 و 20 لكل محاضرة.")
                return
            num_questions_per_pdf = [num_q] * num_pdfs_uploaded
        elif len(num_questions_args) == num_pdfs_uploaded:
            num_questions_per_pdf = [int(arg) for arg in num_questions_args]
            if any(n <= 0 or n > 20 for n in num_questions_per_pdf):
                await message.reply_text("⚠️ عدد الأسئلة لكل محاضرة يجب أن يكون بين 1 و 20.")
                return
        else:
            await message.reply_text(
                f"لقد أرسلت {num_pdfs_uploaded} محاضرة/محاضرات.\n"
                f"تحتاج لتحديد إما عددًا واحدًا من الأسئلة (يطبق على الكل) أو {num_pdfs_uploaded} أعداد من الأسئلة باستخدام الأمر:\n"
                f"`/generate N1 N2 ...`" # توجيه لاستخدام الأمر النصي للتحديد المخصص
                 "\n\nأو يمكنك اختيار أحد أزرار الإنشاء السريع (مثل 5 أو 10 للكل).",
                parse_mode='Markdown'
            )
            return
    except ValueError:
        await message.reply_text("⚠️ من فضلك أدخل أرقامًا صحيحة لعدد الأسئلة.")
        return

    await message.reply_text(f"⏳ جاري إنشاء الأسئلة لـ {num_pdfs_uploaded} محاضرة/محاضرات...")

    all_results = []
    generation_errors = 0

    for i, pdf_data in enumerate(context.user_data[chat_id]['pdfs']):
        lecture_name = pdf_data['filename']
        lecture_text = pdf_data['text']
        num_q_for_this_lecture = num_questions_per_pdf[i]

        if "Error:" in lecture_text or not lecture_text.strip():
            error_msg = lecture_text if "Error:" in lecture_text else "النص المستخرج فارغ."
            result = (f"---  LECTURE: {lecture_name} ---\n"
                     f"❌ لا يمكن إنشاء أسئلة لهذه المحاضرة: {error_msg}")
            all_results.append(result)
            logger.warning(f"Skipping MCQ generation for {lecture_name} due to text extraction issue: {error_msg}")
            generation_errors += 1
            continue

        # إظهار إشعار بالكتابة وتحديث حالة المعالجة
        await context.bot.send_chat_action(chat_id=chat_id, action='typing')
        status_message_text = f"({i+1}/{num_pdfs_uploaded}) ⚙️ جاري إنشاء {num_q_for_this_lecture} سؤال/أسئلة لـ: {lecture_name}..."
        # لا نرسل رسالة حالة لكل ملف لتجنب الإزعاج، نعتمد على الرسالة الأولية
        logger.info(status_message_text)


        mcqs_text = await generate_mcqs_from_text(lecture_text, num_q_for_this_lecture, lecture_name)

        response_header = f"--- ✅ LECTURE: {lecture_name} ---\n\n"
        # التحقق مما إذا كانت الاستجابة تحتوي على خطأ من الدالة نفسها
        if "Sorry, I couldn't generate MCQs" in mcqs_text or "blocked by safety filters" in mcqs_text:
             response_header = f"--- ⚠️ LECTURE: {lecture_name} ---\n\n"
             generation_errors += 1


        full_response = response_header + mcqs_text
        all_results.append(full_response)

        # تأخير بسيط
        await asyncio.sleep(1.5)

    # إرسال النتائج مجمعة
    await message.reply_text("--- 📝 نتائج إنشاء الأسئلة ---")
    combined_message = ""
    for result in all_results:
        if len(combined_message) + len(result) + 2 > 4096: # +2 for potential newlines
            try:
                await message.reply_text(combined_message)
            except Exception as send_error:
                logger.error(f"Error sending combined message part: {send_error}")
                await message.reply_text("خطأ في إرسال جزء من النتائج.")
            combined_message = result + "\n\n"
        else:
            combined_message += result + "\n\n"

    if combined_message:
        try:
            await message.reply_text(combined_message.strip())
        except Exception as send_error:
             logger.error(f"Error sending final combined message part: {send_error}")
             await message.reply_text("خطأ في إرسال الجزء الأخير من النتائج.")

    final_message = "🏁 تم الانتهاء من محاولة إنشاء جميع الأسئلة المطلوبة!"
    if generation_errors > 0:
        final_message += f"\n\nلاحظ: واجهت مشكلة في إنشاء الأسئلة لـ {generation_errors} محاضرة/محاضرات (يرجى مراجعة النتائج أعلاه)."

    final_message += "\n\n**يرجى مراجعة الأسئلة للتأكد من دقتها وملاءمتها.**"
    final_message += "\n\nيمكنك رفع المزيد من المحاضرات أو مسح القائمة والبدء من جديد."
    await message.reply_text(final_message)
    # لا نمسح الملفات تلقائيًا بعد الآن، ليبقى المستخدم متحكمًا عبر زر المسح


# --- دالة إنشاء لوحة المفاتيح الرئيسية ---
def build_main_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> InlineKeyboardMarkup:
    """ينشئ ويعيد لوحة المفاتيح الرئيسية."""
    buttons = [
        [InlineKeyboardButton("📄 إرسال ملفات المحاضرات (PDF)", callback_data=CALLBACK_UPLOAD_INFO)],
        [
            InlineKeyboardButton("⚡ إنشاء 5 أسئلة/محاضرة", callback_data=CALLBACK_GENERATE_5),
            InlineKeyboardButton("⚡ إنشاء 10 أسئلة/محاضرة", callback_data=CALLBACK_GENERATE_10)
        ],
        [InlineKeyboardButton("⚙️ تحديد عدد الأسئلة (عبر أمر نصي)", callback_data=CALLBACK_GENERATE_SPECIFY)],
        [InlineKeyboardButton("🗑️ مسح المحاضرات المرفوعة", callback_data=CALLBACK_CLEAR)]
    ]
    # عرض عدد المحاضرات المرفوعة حاليًا إن وجد
    pdf_count = 0
    if chat_id in context.user_data and 'pdfs' in context.user_data[chat_id]:
        pdf_count = len(context.user_data[chat_id]['pdfs'])

    # لا نضيف الزر إذا كان العدد صفرًا - لتجنب تكرار زر المسح
    # if pdf_count > 0:
    #     buttons.append([InlineKeyboardButton(f"🗑️ مسح ({pdf_count}) محاضرات مرفوعة", callback_data=CALLBACK_CLEAR)])
    # else:
        # يمكن إضافة زر آخر هنا إذا لم تكن هناك ملفات
        # pass

    return InlineKeyboardMarkup(buttons)

# --- معالج الأوامر ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل رسالة ترحيب ولوحة المفاتيح الرئيسية."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"User {user.first_name} (ID: {user.id}) started the bot in chat {chat_id}")

    # تهيئة بيانات المستخدم إذا لم تكن موجودة
    if chat_id not in context.user_data:
        context.user_data[chat_id] = {'pdfs': []}

    keyboard = build_main_keyboard(context, chat_id)
    await update.message.reply_html(
        rf"أهلاً بك يا {user.mention_html()}!",
        reply_markup=keyboard
    )
    await update.message.reply_text(
         "أنا بوت إنشاء أسئلة الاختيار من متعدد (MCQs) باستخدام Google AI (Gemini).\n\n"
         "**كيفية الاستخدام:**\n"
         "1. اضغط على زر '📄 إرسال ملفات...' لمعرفة كيفية الرفع.\n"
         "2. أرسل ملف أو أكثر من ملفات PDF للمحاضرات.\n"
         "3. بعد الانتهاء من الرفع، اختر أحد أزرار 'إنشاء الأسئلة' (5 أو 10 للكل).\n"
         "4. أو، اضغط '⚙️ تحديد عدد الأسئلة' واتبع التعليمات لإرسال أمر نصي مثل `/generate 3 7` (3 للأولى، 7 للثانية).\n"
         "5. استخدم زر '🗑️ مسح' لمسح المحاضرات المرفوعة والبدء من جديد."
    )


# --- معالج نقرات الأزرار ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج نقرات الأزرار المضمنة."""
    query = update.callback_query
    await query.answer() # مهم جدًا لإيقاف إشارة التحميل على الزر

    callback_data = query.data
    chat_id = query.message.chat_id
    logger.info(f"Callback query received: {callback_data} from chat_id {chat_id}")

    if callback_data == CALLBACK_UPLOAD_INFO:
        await query.message.reply_text(
            "📤 يرجى إرسال ملفات PDF التي تريد إنشاء أسئلة لها مباشرة في هذه الدردشة. يمكنك إرسال عدة ملفات واحدًا تلو الآخر."
        )
        # يمكن إعادة إرسال لوحة المفاتيح الرئيسية إذا أردنا أن تبقى ظاهرة
        # keyboard = build_main_keyboard(context, chat_id)
        # await query.edit_message_reply_markup(reply_markup=keyboard) # تحرير الرسالة الأصلية لإبقاء الأزرار

    elif callback_data == CALLBACK_CLEAR:
        clear_message = await _perform_clear(chat_id, context)
        # تحرير الرسالة الأصلية لعرض النتيجة وتحديث الأزرار
        keyboard = build_main_keyboard(context, chat_id)
        try:
            await query.edit_message_text(
                text=f"{query.message.text}\n\n---\n{clear_message}", # إضافة النتيجة للرسالة السابقة
                reply_markup=keyboard,
                parse_mode='HTML' # إذا كانت الرسالة الأصلية HTML
            )
        except Exception as e: # قد تفشل إذا لم تتغير الرسالة أو لوحة المفاتيح
            logger.warning(f"Failed to edit message after clear: {e}. Sending new message.")
            await query.message.reply_text(clear_message, reply_markup=keyboard) # إرسال رسالة جديدة كبديل


    elif callback_data == CALLBACK_GENERATE_5:
        await _perform_generation(update, context, ['5'])

    elif callback_data == CALLBACK_GENERATE_10:
         await _perform_generation(update, context, ['10'])

    elif callback_data == CALLBACK_GENERATE_SPECIFY:
        await query.message.reply_text(
            "ℹ️ لتحديد عدد أسئلة مختلف لكل محاضرة، استخدم الأمر النصي بعد رفع جميع الملفات.\n"
            "مثال: إذا رفعت محاضرتين وتريد 3 أسئلة للأولى و 7 للثانية، أرسل:\n"
            "`/generate 3 7`\n\n"
            "إذا أردت نفس العدد (مثلاً 4) لكل المحاضرات، أرسل:\n"
            "`/generate 4`",
            parse_mode='Markdown'
        )
        # يمكن تحديث لوحة المفاتيح هنا أيضًا إذا أردنا
        # keyboard = build_main_keyboard(context, chat_id)
        # await query.edit_message_reply_markup(reply_markup=keyboard)


# --- معالج إرسال الملفات ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج ملفات PDF المرسلة."""
    chat_id = update.effective_chat.id
    document = update.message.document
    user = update.effective_user

    if document.mime_type == 'application/pdf':
        if chat_id not in context.user_data or 'pdfs' not in context.user_data[chat_id]:
            context.user_data[chat_id] = {'pdfs': []} # ضمان التهيئة

        # إظهار رسالة بأن الملف قيد المعالجة
        processing_msg = await update.message.reply_text(f"⏳ جاري معالجة الملف: {document.file_name}...")

        file = await document.get_file()
        temp_dir = "temp_pdfs"
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.join(temp_dir, f"{chat_id}_{document.file_unique_id}_{document.file_name}")

        try:
            await file.download_to_drive(custom_path=file_path)
            logger.info(f"Downloaded PDF: {file_path} from user {user.id}")
        except Exception as download_error:
            logger.error(f"Failed to download file {document.file_name} from user {user.id}: {download_error}")
            await processing_msg.edit_text(f"❌ عذراً، لم أتمكن من تحميل الملف: {document.file_name}.")
            return

        extracted_text = extract_text_from_pdf(file_path)

        final_status_message = ""
        if "Error:" in extracted_text:
            final_status_message = f"❌ حدث خطأ أثناء معالجة {document.file_name}: {extracted_text}"
            logger.error(f"Text extraction error for {document.file_name} (user {user.id}): {extracted_text}")
        elif not extracted_text.strip():
             final_status_message = f"⚠️ لم يتم استخراج أي نص من الملف {document.file_name}. قد يكون الملف صورة أو فارغاً."
             logger.warning(f"No text extracted from {document.file_name} (user {user.id}).")
        else:
            context.user_data[chat_id]['pdfs'].append({
                'filename': document.file_name,
                'text': extracted_text
            })
            pdf_count = len(context.user_data[chat_id]['pdfs'])
            final_status_message = (
                f"✅ تمت إضافة الملف بنجاح: {document.file_name}.\n"
                f"📚 إجمالي المحاضرات المرفوعة الآن: {pdf_count}."
            )
            logger.info(f"Successfully processed {document.file_name} for user {user.id}. Total PDFs: {pdf_count}")

        # تحرير رسالة المعالجة لعرض النتيجة النهائية
        await processing_msg.edit_text(final_status_message)

        # حذف الملف المؤقت
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Removed temporary file: {file_path}")
            except Exception as remove_error:
                logger.error(f"Failed to remove temporary file {file_path}: {remove_error}")

        # إرسال لوحة المفاتيح مجددًا بعد رفع الملف لتحديث الأزرار (اختياري)
        # keyboard = build_main_keyboard(context, chat_id)
        # await update.message.reply_text("يمكنك الآن إرسال ملف آخر أو إنشاء الأسئلة.", reply_markup=keyboard)

    else:
        await update.message.reply_text("⚠️ من فضلك أرسل ملف PDF فقط.")


# --- معالج الأمر النصي generate (للتحديد المخصص) ---
async def generate_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
     """يعالج الأمر النصي /generate N1 N2 ..."""
     logger.info(f"Received /generate command with args: {context.args} from user {update.effective_user.id}")
     if not context.args:
         await update.message.reply_text(
             "⚠️ لاستخدام الأمر النصي، يرجى تحديد عدد الأسئلة.\n"
            "مثال: `/generate 5` (5 أسئلة لكل محاضرة)\n"
            "أو `/generate 3 7` (3 للمحاضرة الأولى، 7 للثانية).",
             parse_mode='Markdown'
         )
         return
     await _perform_generation(update, context, context.args)


# --- معالج الأوامر غير المعروفة ---
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("عذراً، لم أفهم هذا الأمر. استخدم الأزرار أو الأمر `/start` للبدء.")

# --- الدالة الرئيسية ---
def main() -> None:
    """يبدأ تشغيل البوت."""
    if not model:
        logger.error("Google AI Model is not initialized. Aborting bot startup.")
        print("Error: Google AI Model failed to initialize. Please check your API key and configuration.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # إضافة معالجات الأوامر والأزرار والرسائل
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("generate", generate_command_handler)) # <-- معالج الأمر النصي
    # لم نعد بحاجة لمعالج أمر /clear منفصل، يتم التعامل معه عبر الزر
    # application.add_handler(CommandHandler("clear", clear_pdfs_command_handler))

    application.add_handler(CallbackQueryHandler(button_callback)) # <-- معالج الأزرار

    application.add_handler(MessageHandler(filters.Document.PDF, handle_document)) # <-- معالج ملفات PDF
    application.add_handler(MessageHandler(filters.Document.ALL & (~filters.Document.PDF),
        lambda update, context: update.message.reply_text("⚠️ من فضلك أرسل ملفات PDF فقط."))) # معالج ملفات غير PDF

    # معالج للأوامر غير المعروفة (يفضل أن يكون الأخير)
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))


    logger.info("Bot is starting with Inline Keyboard and Google AI...")
    application.run_polling()

if __name__ == "__main__":
    # --- الكود الخاص بـ Google AI (generate_mcqs_from_text) يبقى كما هو ---
    async def generate_mcqs_from_text(lecture_text: str, num_questions: int, lecture_name: str) -> str:
        """ينشئ أسئلة MCQ باستخدام Google Gemini API."""
        if not model:
            return f"Sorry, the Google AI model client is not initialized. Cannot generate MCQs for '{lecture_name}'."

        MAX_TEXT_LENGTH = 50000
        original_length = len(lecture_text)
        if original_length > MAX_TEXT_LENGTH:
            lecture_text = lecture_text[:MAX_TEXT_LENGTH]
            warning_msg = (f"\n\n(ملاحظة: تم اقتصاص نص المحاضرة '{lecture_name}' "
                           f"من {original_length} إلى {MAX_TEXT_LENGTH} حرفاً "
                           f"لتجنب المشاكل المحتملة. سيتم إنشاء الأسئلة من الجزء الأول فقط.)")
        else:
            warning_msg = ""

        prompt = f"""You are an expert AI assistant specializing in creating high-quality educational multiple-choice questions (MCQs).
Your task is to generate exactly {num_questions} MCQs in English based ONLY on the provided lecture content from "{lecture_name}".

**Instructions:**
1.  Read the lecture content carefully.
2.  Create {num_questions} distinct MCQs that test understanding of the key concepts in the text.
3.  Each MCQ must follow this specific format STRICTLY:
    [Question Number]. [Question Text]
    A) [Option A]
    B) [Option B]
    C) [Option C]
    D) [Option D]
    Answer: [Correct Letter]
4.  Ensure there are exactly four options (A, B, C, D) for each question.
5.  Clearly indicate the single correct answer using "Answer: [Letter]".
6.  **Crucially:** Do NOT include any introductory sentences, concluding remarks, explanations, apologies, or any text whatsoever outside of the MCQ questions themselves in the specified format. Your entire output should consist only of the numbered questions, options, and answers.

**Lecture Content:**
--- START OF CONTENT ---
{lecture_text}
--- END OF CONTENT ---

Generate the MCQs now:
"""

        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ]

        generation_config = genai.types.GenerationConfig(
            temperature=0.5
        )

        retries = 2
        for attempt in range(retries):
            try:
                response = await asyncio.to_thread(
                    model.generate_content,
                    contents=prompt,
                    generation_config=generation_config,
                    safety_settings=safety_settings
                )

                if not response.candidates:
                     block_reason = "Unknown safety block"
                     try:
                         block_reason = response.prompt_feedback.block_reason.name
                     except Exception:
                         pass
                     error_message = f"Sorry, the request for '{lecture_name}' was blocked by safety filters (Reason: {block_reason}). This might happen with sensitive topics. Try modifying the content if possible."
                     logger.warning(f"Safety block for '{lecture_name}': {block_reason}. Prompt feedback: {response.prompt_feedback}")
                     return error_message

                mcqs = response.text.strip()

                if not mcqs or not ("Answer:" in mcqs or "A)" in mcqs):
                     logger.warning(f"Gemini response for '{lecture_name}' seems empty or invalid: '{mcqs[:100]}...'")
                     if attempt < retries - 1:
                         await asyncio.sleep(2)
                         continue
                     else:
                        return f"Sorry, the AI model returned an unexpected or empty response for '{lecture_name}' after {retries} attempts. Response snippet: '{mcqs[:100]}...'"

                # التحقق الإضافي من التنسيق (اختياري ولكنه قد يساعد)
                # التأكد من أن الناتج يبدأ برقم أو يحتوي على 'Answer:'
                lines = mcqs.splitlines()
                if not lines or not (lines[0].strip().startswith(('1.', '2.', '3.')) or 'Answer:' in lines[0]):
                     logger.warning(f"Gemini response for '{lecture_name}' does not seem to follow the expected MCQ format. Snippet: '{mcqs[:150]}...'")
                     # لا نعتبره خطأ فادحًا بالضرورة، لكن نسجل تحذيرًا

                return mcqs + warning_msg

            except Exception as e:
                logger.error(f"Google AI API error (Attempt {attempt+1}/{retries}) for lecture {lecture_name}: {e}")
                if attempt < retries - 1:
                    wait_time = (attempt + 1) * 3
                    logger.info(f"API error occurred. Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    error_details = str(e)
                    user_message = f"Sorry, I couldn't generate MCQs for '{lecture_name}'. An error occurred after {retries} attempts."
                    if "API key not valid" in error_details:
                        user_message += " Please check if the Google API Key in the code is correct."
                    elif "quota" in error_details.lower():
                         user_message += " You might have exceeded the usage limits for the free tier. Please check your Google AI Studio usage."
                    elif "429" in error_details or "resource_exhausted" in error_details.lower():
                         user_message += " The service is currently busy or rate limits exceeded. Please try again in a moment."
                    else:
                         user_message += f" Error details: {error_details[:100]}..."
                    return user_message

        return f"Sorry, I couldn't generate MCQs for '{lecture_name}' after {retries} attempts due to repeated errors."
    # ---------------------------------------------------------------------
    main() # <-- استدعاء الدالة الرئيسية لبدء البوت
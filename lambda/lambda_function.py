import asyncio
import logging
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO

import boto3
import templates
from cache import chords, mp3, piano, scores, songs, titles
from lookup import songs_lookup, titles_lookup
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Inches, Pt
from rapidfuzz import fuzz, process
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    constants,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from unidecode import unidecode

logger = logging.getLogger()
logger.setLevel("INFO")

app = Application.builder().token(os.getenv("BOT_TOKEN")).build()
dynamodb = boto3.resource("dynamodb")


def saveLog(user, event, request, response):
    dynamodb.Table("tsms_logs").put_item(
        Item={
            "user_id": user.id,
            "name": user.full_name,
            "username": user.username,
            "timestamp": Decimal(str(datetime.utcnow().timestamp())),
            "timestamp_iso": datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "event": event,
            "request": request,
            "response": response,
        }
    )


def validUser(user):
    dbUser = dynamodb.Table("tsms_users").get_item(Key={"id": user.id})
    return bool(dbUser)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    contact_keyboard = KeyboardButton(text="REGISTER", request_contact=True)
    reply_markup = ReplyKeyboardMarkup(
        [[contact_keyboard]],
        one_time_keyboard=True,
        input_field_placeholder="Use button below to register",
    )
    await update.message.reply_html(templates.start, reply_markup=reply_markup)
    saveLog(user, "START", None, None)


async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    contact = update.message.contact
    phone = contact.phone_number
    if user.id == contact.user_id and phone.startswith(templates.allowed_phone):
        dynamodb.Table("tsms_users").put_item(
            Item={
                "id": user.id,
                "name": user.full_name,
                "phone": phone,
                "state": "v1",
            }
        )
        await update.message.reply_html(
            templates.welcome, reply_markup=ReplyKeyboardRemove()
        )
        saveLog(user, "CONTACT", phone, "Allowed")
    else:
        await update.message.reply_html(
            "Sorry, our automated security check does not allow you to use this bot.",
            reply_markup=ReplyKeyboardRemove(),
        )
        saveLog(user, "CONTACT", phone, "Blocked")


async def help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not validUser(user):
        await update.effective_chat.send_message("Press /start to begin")
        return
    await update.message.reply_html(
        templates.examples, reply_markup=ReplyKeyboardRemove(), protect_content=True
    )
    saveLog(user, "HELP", None, None)


def make_button(song_number, option, callback_prefix, button_text):
    if option:
        return [
            [
                InlineKeyboardButton(
                    button_text, callback_data=f"{callback_prefix} {song_number}"
                )
            ]
        ]
    return []


async def send_song(update: Update, song_number) -> None:
    keyboard = []
    keyboard.extend(
        make_button(song_number, song_number in chords, "CHORDS", "🎸 Guitar Chords")
    )
    keyboard.extend(
        make_button(song_number, song_number in scores, "SCORE", "🎼 Piano Score")
    )
    keyboard.extend(
        make_button(song_number, song_number in mp3, "MP3", "🔊 MIDI Soundtrack")
    )
    keyboard.extend(
        make_button(
            titles[song_number],
            titles[song_number] in piano,
            "PIANO",
            "🎹 Piano Recording (Wilds)",
        )
    )
    keyboard.extend(make_button(song_number, True, "PPT", "💻 Generate PowerPoint"))
    await update.effective_chat.send_message(
        text=songs.get(song_number),
        parse_mode=constants.ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not validUser(user):
        await update.effective_chat.send_message("Press /start to begin")
        return
    raw_message = update.message.text
    message = unidecode(raw_message).replace("\n", " ").strip().upper()
    if message.isnumeric():  # handle default book
        message = int(message)
        message = "TSMS " + str(message)
    song_number = None
    results = []
    if message in songs:  # match song number
        song_number = message
    else:  # match title
        alpha_only = re.compile("[^A-Z ]")
        clean_message = alpha_only.sub("", message).strip()
        if clean_message in titles_lookup:
            results = titles_lookup.get(clean_message)
            song_number = results.pop(0)
        else:  # search
            if len(clean_message) > 200:
                await update.message.reply_html("<i>Please shorten your search</i>")
                saveLog(user, "SEARCH_TOO_LONG", raw_message, None)
                return
            await update.message.reply_chat_action(constants.ChatAction.TYPING)
            query = process.extract(
                clean_message,
                songs_lookup,
                scorer=fuzz.partial_ratio,
                score_cutoff=85,
                limit=10,
            )
            results = [t[2] for t in query]
    if song_number:
        await send_song(update, song_number)
    if results:
        keyboard = []
        for number in results:
            keyboard.extend(
                make_button(number, True, "SONG", f"{number} {titles[number]}")
            )
        if song_number:
            await update.message.reply_html(
                "<i>This song is also found in:</i>",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            await update.message.reply_html(
                "<i>Showing up to 10 search results:</i>",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            saveLog(user, "SEARCH_RESULTS", raw_message, f"{len(results)} results")
    else:
        if song_number:
            saveLog(
                user, "SEARCH_HIT", raw_message, f"{song_number} {titles[song_number]}"
            )
        else:
            await update.message.reply_html(
                "<i>No matches found</i>\n\nType /help for instructions"
            )
            saveLog(user, "SEARCH_NONE", raw_message, None)


def make_ppt(song_number):
    prs = Presentation()
    prs.slide_width = Inches(16)
    prs.slide_height = Inches(9)

    title = titles.get(song_number)
    text = songs.get(song_number)
    text = text.split("\n\n")
    text.pop(0)
    text = list(filter(None, text))

    originallen = len(text)
    chorus = None
    for i in range(originallen):
        stanza = text[i]
        if stanza.startswith("Chorus:") or stanza.startswith("Refrain:"):
            chorus = i
            break
    if chorus:
        i = chorus + 2
        while True:
            text.insert(i, stanza)
            i += 2
            if i > len(text):
                break

    blank_slide_layout = prs.slide_layouts[6]
    slide = prs.slides.add_slide(blank_slide_layout)
    background = slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = RGBColor(0, 0, 0)

    txBox = slide.shapes.add_textbox(0, 0, Inches(16), Inches(9))
    tf = txBox.text_frame
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.add_paragraph()
    p.text = title + "\n(" + song_number + ")"
    p.font.size = Pt(60)
    p.font.bold = True
    p.font.color.rgb = RGBColor(255, 255, 255)
    p.alignment = PP_ALIGN.CENTER

    for i in range(len(text)):
        blank_slide_layout = prs.slide_layouts[6]
        slide = prs.slides.add_slide(blank_slide_layout)
        background = slide.background
        fill = background.fill
        fill.solid()
        fill.fore_color.rgb = RGBColor(0, 0, 0)

        txBox = slide.shapes.add_textbox(Inches(15), 0, Inches(1), Inches(1))
        tf = txBox.text_frame
        p = tf.add_paragraph()
        p.text = "{}/{}".format(i + 1, len(text))
        p.font.size = Pt(32)
        p.font.color.rgb = RGBColor(255, 255, 255)

        txBox = slide.shapes.add_textbox(0, 0, Inches(16), Inches(9))
        tf = txBox.text_frame
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.word_wrap = True
        tf.auto_size = MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT
        p = tf.add_paragraph()
        p.text = text[i].strip()
        p.font.size = Pt(48)
        if song_number.startswith("C "):
            p.font.size = Pt(32)
        p.font.bold = True
        p.font.color.rgb = RGBColor(255, 255, 255)
        p.alignment = PP_ALIGN.CENTER
    pptxfile = BytesIO()
    pptxfile.name = song_number + ".pptx"
    prs.save(pptxfile)
    pptxfile.seek(0)
    return pptxfile


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not validUser(user):
        await update.effective_chat.send_message(text="Press /start to begin")
        return
    query = update.callback_query
    data = query.data
    if data.startswith("SONG "):
        song_number = data.replace("SONG ", "")
        await send_song(update, song_number)
        saveLog(user, "CALLBACK", "SONG", song_number)
    elif data.startswith("CHORDS "):
        song_number = data.replace("CHORDS ", "")
        await update.effective_chat.send_message(
            text=f"<pre>{chords[song_number]}</pre>",
            parse_mode=constants.ParseMode.HTML,
        )
        saveLog(user, "CALLBACK", "CHORDS", song_number)
    elif data.startswith("SCORE "):
        song_number = data.replace("SCORE ", "")
        await update.effective_chat.send_action(constants.ChatAction.UPLOAD_PHOTO)
        saveLog(user, "CALLBACK", "SCORE", song_number)
        counter = 1
        for i in range(len(scores[song_number])):
            reference = scores[song_number][i]
            title = f"{song_number} {titles[song_number]}"
            if len(scores[song_number]) > 1:
                title += " " + str(counter)
            await update.effective_chat.send_photo(
                photo=reference, caption=title, protect_content=True
            )
    elif data.startswith("MP3 "):
        song_number = data.replace("MP3 ", "")
        await update.effective_chat.send_action(constants.ChatAction.UPLOAD_DOCUMENT)
        saveLog(user, "CALLBACK", "MP3", song_number)
        counter = 1
        for i in range(len(mp3[song_number])):
            reference = mp3[song_number][i]
            title = f"{song_number} {titles[song_number]}"
            if len(mp3[song_number]) > 1:
                title += " " + str(counter)
            await update.effective_chat.send_audio(
                audio=reference, caption=title, protect_content=True
            )
    elif data.startswith("PIANO "):
        song_title = data.replace("PIANO ", "")
        await update.effective_chat.send_action(constants.ChatAction.UPLOAD_DOCUMENT)
        saveLog(user, "CALLBACK", "PIANO", song_title)
        reference = piano[song_title]
        await update.effective_chat.send_audio(
            audio=reference, caption=song_title, protect_content=True
        )
    elif data.startswith("PPT "):
        song_number = data.replace("PPT ", "")
        await update.effective_chat.send_action(constants.ChatAction.UPLOAD_DOCUMENT)
        saveLog(user, "CALLBACK", "PPT", song_number)
        ppt = make_ppt(song_number)
        await update.effective_chat.send_document(document=ppt)
    else:
        await query.answer(text="This feature is not available")
        saveLog(user, "CALLBACK_INVALID", data, None)
        return
    await query.answer()


async def tg_bot_main(bot_app, event):
    async with bot_app:
        logger.info("PROCESSING UPDATE: %s", event)
        await bot_app.process_update(Update.de_json(event, bot_app.bot))


def lambda_handler(event, context):
    try:
        asyncio.run(tg_bot_main(app, event))
    except Exception as e:
        traceback.print_exc()
        return {"statusCode": 500}
    return {"statusCode": 200}


app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.CONTACT, contact))
app.add_handler(CommandHandler("help", help))
app.add_handler(MessageHandler(filters.TEXT, search))
app.add_handler(CallbackQueryHandler(answer_callback))

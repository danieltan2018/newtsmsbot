import asyncio
import base64
import hmac
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from urllib.parse import quote
from uuid import uuid4

import ai
import boto3
import groups
import templates
from cache import CA_LINKS, CHORDS, MP3, PIANO, SCORES, SGM_LINKS, SONGS, TITLES, VIDEOS
from db import table
from lookup import SONGS_LOOKUP, TITLES_LOOKUP
from rapidfuzz import fuzz, process
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaPhoto,
    KeyboardButton,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    constants,
)
from telegram.error import Forbidden, TelegramError
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Public prefix for externally hosted choir videos.
VIDEOS_URL_PREFIX = os.getenv("VIDEOS_URL_PREFIX", "")
# Empty disables webhook-secret validation for legacy integrations.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# Queue Function URL updates before acknowledging Telegram; fall back inline
# if queueing fails.
ASYNC_HANDOFF = os.getenv("ASYNC_HANDOFF", "1") != "0"
ASYNC_KEY = "asyncUpdate"
COMMUNITY_ENABLED = os.getenv("COMMUNITY_ENABLED", "false").lower() == "true"

ALPHA_ONLY = re.compile("[^A-Z ]")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

app = Application.builder().token(BOT_TOKEN).build()


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
def saveLog(user, event, request, response):
    now = datetime.now(timezone.utc)
    table("logs").put_item(
        Item={
            "user_id": user.id,
            "name": user.full_name,
            "username": user.username,
            "timestamp": Decimal(str(now.timestamp())),
            "timestamp_iso": now.astimezone(timezone(timedelta(hours=8))).isoformat(),
            "event": event,
            "request": request,
            "response": response,
        }
    )


def getDbUser(user):
    return table("users").get_item(Key={"id": user.id})


def video_url(obj):
    # Object names may contain spaces or reserved characters.
    return VIDEOS_URL_PREFIX + quote(obj)


# --------------------------------------------------------------------------- #
# Reply helpers
# --------------------------------------------------------------------------- #
async def require_registration(update: Update, dbUser, action) -> bool:
    if "Item" in dbUser:
        return True
    await update.effective_chat.send_message(templates.not_registered)
    saveLog(update.effective_user, "NOT_REGISTERED", action, None)
    return False


async def reply_and_log(update: Update, text, event, request=None, response=None, **kwargs):
    await update.message.reply_html(text, **kwargs)
    saveLog(update.effective_user, event, request, response)


async def send_and_log(update: Update, text, event, request=None, response=None, **kwargs):
    await update.effective_chat.send_message(
        text=text, parse_mode=constants.ParseMode.HTML, **kwargs
    )
    saveLog(update.effective_user, event, request, response)


async def updateState(update: Update, dbUser) -> None:
    user = update.effective_user
    phone = dbUser["Item"]["phone"].lstrip("+")
    state = dbUser["Item"]["state"]
    if state != templates.current_version:
        table("users").update_item(
            Key={"id": user.id},
            UpdateExpression="SET phone = :phone, #st = :state",
            ExpressionAttributeValues={
                ":phone": phone,
                ":state": templates.current_version,
            },
            ExpressionAttributeNames={"#st": "state"},
        )
        await reply_and_log(update, templates.changelog, "CHANGELOG", response=templates.current_version)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact_keyboard = KeyboardButton(text="REGISTER", request_contact=True)
    reply_markup = ReplyKeyboardMarkup(
        [[contact_keyboard]],
        one_time_keyboard=True,
        input_field_placeholder="Use button below to register",
    )
    await reply_and_log(update, templates.start, "START", reply_markup=reply_markup)


async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    contact = update.message.contact
    phone = contact.phone_number.lstrip("+")
    if user.id == contact.user_id and phone.startswith(templates.allowed_phone):
        # Preserve Community membership when re-registering.
        table("users").update_item(
            Key={"id": user.id},
            UpdateExpression="SET #n = :name, phone = :phone, #st = :state",
            ExpressionAttributeNames={"#n": "name", "#st": "state"},
            ExpressionAttributeValues={
                ":name": user.full_name,
                ":phone": phone,
                ":state": templates.current_version,
            },
        )
        await reply_and_log(
            update, templates.welcome, "CONTACT", request=phone, response="Allowed",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await reply_and_log(
            update,
            "Sorry, our automated security check does not allow you to use this bot.",
            "CONTACT", request=phone, response="Blocked",
            reply_markup=ReplyKeyboardRemove(),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    dbUser = getDbUser(update.effective_user)
    if not await require_registration(update, dbUser, "HELP"):
        return
    await reply_and_log(
        update, templates.examples, "HELP",
        reply_markup=ReplyKeyboardRemove(), protect_content=True,
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_and_log(
        update, templates.unknown_command, "UNKNOWN_COMMAND", request=update.message.text
    )


async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not COMMUNITY_ENABLED:
        await update.message.reply_html(templates.community_disabled)
        logger.info("Community action disabled user=%s action=LEAVE", user.id)
        return
    dbUser = getDbUser(user)
    if not await require_registration(update, dbUser, "LEAVE"):
        return
    active_group_id = groups.get_active_group(user.id, dbUser)
    if active_group_id is None:
        await update.message.reply_html(templates.group_not_in_community)
        logger.info("Community leave blocked user=%s reason=NOT_IN_COMMUNITY", user.id)
        return
    groups.leave_group(user.id, active_group_id, saveLog, user)
    await update.message.reply_html(templates.group_left)


# --------------------------------------------------------------------------- #
# Song display and search
# --------------------------------------------------------------------------- #
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


async def send_link_buttons(update: Update, links, source, song_number) -> None:
    keyboard = [
        [InlineKeyboardButton(key, url=value)] for key, value in links.items()
    ]
    await send_and_log(
        update, f"Links provided by {source}", "LINKS", request=source, response=song_number,
        link_preview_options=NO_PREVIEW,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_song(
    update: Update, context: ContextTypes.DEFAULT_TYPE, song_number, dbUser, event, request
) -> None:
    active_group_id = (
        groups.get_active_group(update.effective_user.id, dbUser) if COMMUNITY_ENABLED else None
    )
    title = TITLES[song_number]
    lyrics = SONGS[song_number]
    keyboard = []
    keyboard.extend(make_button(song_number, song_number in CHORDS, "CHORDS", "🎸 Guitar Chords"))
    keyboard.extend(make_button(song_number, song_number in SCORES, "SCORE", "🎼 Piano Score"))
    keyboard.extend(make_button(song_number, song_number in MP3, "MP3", "🔊 MIDI Soundtrack"))
    keyboard.extend(make_button(title, title in PIANO, "PIANO", "🎹 Piano Recording (Wilds)"))
    keyboard.extend(make_button(title, title in VIDEOS, "VIDEO", "🎤 Choir Recording (Lyric Video)"))
    if lyrics.count("\n\n") > 0:
        keyboard.extend(make_button(song_number, True, "PPT", "💻 Generate PowerPoint"))
        keyboard.extend(make_button(song_number, True, "EXPLAIN", "💭 Explain Song"))
    keyboard.extend(
        make_button(song_number, active_group_id is not None, "GROUP_SEND", "📢 Send to Community")
    )
    await update.effective_chat.send_message(
        text=lyrics, parse_mode=constants.ParseMode.HTML,
        link_preview_options=NO_PREVIEW,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    if song_number in CA_LINKS:
        await send_link_buttons(update, CA_LINKS[song_number], "cityalight.com", song_number)
    if song_number in SGM_LINKS:
        await send_link_buttons(update, SGM_LINKS[song_number], "sovereigngracemusic.com", song_number)
    # Preserve the live success-log payload and timing: after lyrics and links
    # have all been sent. SONG callbacks record only the song number.
    response = song_number if event == "CALLBACK" else f"{song_number} {title}"
    saveLog(update.effective_user, event, request, response)
    if COMMUNITY_ENABLED:
        await groups.process_search_event(context, saveLog, update.effective_user, song_number, active_group_id)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    dbUser = getDbUser(user)
    if not await require_registration(update, dbUser, "SEARCH"):
        return
    raw_message = update.message.text
    message = unidecode(raw_message).replace("\n", " ").strip().upper()
    hint = message.startswith("TSMS")
    if message.isnumeric():  # handle default book
        message = "TSMS " + str(int(message))
    song_number = None
    results = []
    if message in SONGS:  # match song number
        song_number = message
    else:  # match title
        clean_message = ALPHA_ONLY.sub("", message).strip()
        if clean_message in TITLES_LOOKUP:
            results = TITLES_LOOKUP.get(clean_message).copy()
            song_number = results.pop(0)
        else:  # search
            if len(clean_message) > 200:
                await reply_and_log(
                    update, "<i>Please shorten your search</i>", "SEARCH_TOO_LONG", request=raw_message
                )
                return
            await update.message.reply_chat_action(constants.ChatAction.TYPING)
            query = process.extract(
                clean_message,
                SONGS_LOOKUP,
                scorer=fuzz.partial_ratio,
                score_cutoff=85,
                limit=10,
            )
            results = [t[2] for t in query]

    if song_number:
        await send_song(update, context, song_number, dbUser, "SEARCH_HIT", raw_message)
        if hint:
            await reply_and_log(
                update,
                "<i>Hint: Get to TSMS songs faster by typing the song number without 'TSMS'</i>",
                "SEARCH_HINT", request=raw_message, response=song_number,
            )
    if results:
        keyboard = []
        for number in results:
            keyboard.extend(make_button(number, True, "SONG", f"{number} {TITLES[number]}"))
        if song_number:
            await reply_and_log(
                update, "<i>This song is also found in:</i>", "SEARCH_ALSO_FOUND",
                request=raw_message, response=f"{len(results)} results",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            await reply_and_log(
                update, "<i>Showing up to 10 search results:</i>", "SEARCH_RESULTS",
                request=raw_message, response=f"{len(results)} results",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
    elif not song_number:
        await reply_and_log(
            update, "<i>No matches found</i>\n\nType /help for instructions", "SEARCH_NONE",
            request=raw_message,
        )
    await updateState(update, dbUser)


# --------------------------------------------------------------------------- #
# PowerPoint
# --------------------------------------------------------------------------- #
def make_ppt(song_number):
    # Keep slide dependencies off the normal cold-start path.
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width = Inches(16)
    prs.slide_height = Inches(9)

    title = TITLES.get(song_number)
    text = SONGS.get(song_number)
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
    if chorus is not None:
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


# --------------------------------------------------------------------------- #
# Callback buttons
# --------------------------------------------------------------------------- #
# Old buttons can refer to songs or media removed by a data update.
CALLBACK_LOOKUP = {
    "SONG": SONGS,
    "CHORDS": CHORDS,
    "SCORE": SCORES,
    "MP3": MP3,
    "PIANO": PIANO,
    "VIDEO": VIDEOS,
    "PPT": SONGS,
    "EXPLAIN": SONGS,
    "GROUP_SEND": SONGS,
}


async def group_send(update: Update, context: ContextTypes.DEFAULT_TYPE, user, dbUser, song_number) -> None:
    query = update.callback_query
    if not COMMUNITY_ENABLED:
        await query.answer(text=templates.community_disabled, show_alert=True)
        logger.info("Community action disabled user=%s action=GROUP_SEND song=%s", user.id, song_number)
        return
    active_group_id = groups.get_active_group(user.id, dbUser)
    correlation = {"broadcast_id": uuid4().hex}
    if active_group_id:
        correlation["community_id"] = active_group_id

    async def blocked(reason, text):
        groups.safe_log(saveLog, user, "COMMUNITY_SEND", song_number,
                        {**correlation, "status": "blocked", "reason": reason})
        await query.answer(text=text, show_alert=True)

    if active_group_id is None:
        await blocked("NOT_IN_COMMUNITY", templates.group_send_expired)
        return
    # Build the message (escaped) before marking the song as sent, so a
    # failure here can never leave the song permanently "already sent".
    lyrics = SONGS[song_number] + templates.group_sent_caption.format(
        name=html.escape(user.full_name)
    )
    members = groups.try_mark_sent(active_group_id, song_number, user.id)
    if members is None:
        await blocked("ALREADY_SENT_OR_INACTIVE", templates.group_send_already)
        return
    recipients = members - {user.id}
    if not recipients:
        groups.unmark_sent(active_group_id, song_number)
        await blocked("NO_RECIPIENTS", templates.group_send_nobody)
        return

    # Counts describe the membership snapshot, not confirmed delivery.
    groups.safe_log(saveLog, user, "COMMUNITY_SEND", song_number,
                    {**correlation, "status": "attempted", "recipient_count": len(recipients)})
    sender = {**correlation, "sender_id": user.id, "sender_name": user.full_name,
              "sender_username": user.username}

    async def send_to_member(member_id):
        recipient = groups.log_user(member_id)
        details = sender
        # This records an attempt, not confirmed delivery or a read receipt.
        groups.safe_log(saveLog, recipient, "COMMUNITY_RECEIVE", song_number, details)
        try:
            await context.bot.send_message(
                chat_id=member_id,
                text=lyrics,
                parse_mode=constants.ParseMode.HTML,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramError as error:
            logger.warning("Community send attempt failed user=%s song=%s error=%s",
                           member_id, song_number, type(error).__name__)
            groups.safe_log(saveLog, recipient, "ERROR", song_number, {
                **details, "action": "COMMUNITY_RECEIVE",
                "error": type(error).__name__,
            })
            if isinstance(error, Forbidden):
                # A failed cleanup must not cancel sends to the other members.
                try:
                    groups.leave_group(member_id, active_group_id, saveLog,
                                       reason="bot_unavailable")
                except Exception as cleanup_error:
                    logger.exception("Failed to remove unavailable Community member %s", member_id)
                    groups.safe_log(saveLog, recipient, "ERROR", song_number, {
                        **details, "action": "COMMUNITY_REMOVE",
                        "error": type(cleanup_error).__name__,
                    })
            return False
        return True

    delivered = sum(await asyncio.gather(*(send_to_member(m) for m in sorted(recipients))))
    if delivered == 0:
        groups.unmark_sent(active_group_id, song_number)
    try:
        await query.answer(text=templates.group_send_done.format(n=delivered, s="" if delivered == 1 else "s"))
    except TelegramError:
        # An expired callback toast must not suppress the broadcast invitation.
        logger.warning("Could not answer Community callback song=%s", song_number)
    # A broadcast song is the invitation: people who look it up now join.
    await groups.invite_recent_searchers(
        context, saveLog, active_group_id, song_number, exclude=members
    )


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    query = update.callback_query
    chat = update.effective_chat
    dbUser = getDbUser(user)
    if "Item" not in dbUser:
        # always answer the query so the button stops spinning.
        await query.answer(text=templates.not_registered, show_alert=True)
        saveLog(user, "NOT_REGISTERED", "CALLBACK", None)
        return

    action, _, arg = (query.data or "").partition(" ")
    lookup = CALLBACK_LOOKUP.get(action)
    if lookup is None:
        await query.answer(text=templates.callback_invalid, show_alert=True)
        saveLog(user, "CALLBACK_INVALID", query.data, None)
        return
    if arg not in lookup:
        await query.answer(text=templates.callback_missing, show_alert=True)
        saveLog(user, "CALLBACK_MISSING", query.data, None)
        return

    if action == "GROUP_SEND":
        await group_send(update, context, user, dbUser, arg)  # answers the query itself
        return

    # Keep the legacy action-log ordering. VIDEO and EXPLAIN show their toast
    # after logging the attempt, before starting the slow operation.
    answered = False

    if action == "SONG":
        await send_song(update, context, arg, dbUser, "CALLBACK", "SONG")
    elif action == "CHORDS":
        await send_and_log(
            update, f"<pre>{html.escape(CHORDS[arg])}</pre>", "CALLBACK", request="CHORDS", response=arg
        )
    elif action == "SCORE":
        await chat.send_action(constants.ChatAction.UPLOAD_PHOTO)
        saveLog(user, "CALLBACK", "SCORE", arg)
        await chat.send_media_group(media=[InputMediaPhoto(media=p) for p in SCORES[arg]])
    elif action == "MP3":
        await chat.send_action(constants.ChatAction.UPLOAD_DOCUMENT)
        saveLog(user, "CALLBACK", "MP3", arg)
        await chat.send_media_group(media=[InputMediaAudio(media=a) for a in MP3[arg]])
    elif action == "PIANO":
        await chat.send_action(constants.ChatAction.UPLOAD_DOCUMENT)
        saveLog(user, "CALLBACK", "PIANO", arg)
        await chat.send_audio(audio=PIANO[arg], protect_content=True)
    elif action == "VIDEO":
        await chat.send_action(constants.ChatAction.UPLOAD_VIDEO)
        saveLog(user, "CALLBACK", "VIDEO", arg)
        objects = VIDEOS[arg]
        total = len(objects)
        await query.answer(text=templates.video_toast.format(n=total, s="" if total == 1 else "s"))
        answered = True
        for count, obj in enumerate(objects, start=1):
            caption = f"{arg.title()} {count}" if total > 1 else arg.title()
            await chat.send_video(video=video_url(obj), caption=caption)
    elif action == "PPT":
        await chat.send_action(constants.ChatAction.UPLOAD_DOCUMENT)
        saveLog(user, "CALLBACK", "PPT", arg)
        await chat.send_document(document=make_ppt(arg))
    elif action == "EXPLAIN":
        await chat.send_action(constants.ChatAction.TYPING)
        saveLog(user, "CALLBACK", "EXPLAIN", arg)
        await query.answer(text=templates.explain_toast)
        answered = True
        response = ai.explainSong(SONGS[arg])
        await chat.send_message(response, parse_mode=constants.ParseMode.HTML)
    if not answered:
        await query.answer()


# --------------------------------------------------------------------------- #
# Error handling and entry point
# --------------------------------------------------------------------------- #
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Notify the user when PTB routes a handler exception here.
    logger.error("Unhandled exception while processing update", exc_info=context.error)
    if not isinstance(update, Update):
        return
    notified = None
    try:
        if update.callback_query:
            await update.callback_query.answer(text=templates.generic_error, show_alert=True)
            notified = "ALERT"
        elif update.effective_chat:
            await update.effective_chat.send_message(templates.generic_error)
            notified = "MESSAGE"
    except TelegramError:
        logger.exception("Failed to notify user about the error")
    if update.effective_user:
        try:
            what = (update.callback_query.data if update.callback_query
                    else getattr(update.effective_message, "text", None))
            saveLog(update.effective_user, "ERROR", (what or "")[:200],
                    f"{type(context.error).__name__}: {str(context.error)[:150]} -> {notified}")
        except Exception:
            logger.exception("Failed to log the error")  # DynamoDB itself may be the cause


def _update_type(update: Update) -> str:
    for kind in ("message", "edited_message", "callback_query"):
        if getattr(update, kind, None):
            return kind
    return "other"


async def tg_bot_main(bot_app, event):
    async with bot_app:
        update = Update.de_json(event, bot_app.bot)
        # Raw updates can contain phone numbers and message text.
        logger.info(
            "PROCESSING UPDATE id=%s user=%s type=%s",
            update.update_id,
            update.effective_user.id if update.effective_user else None,
            _update_type(update),
        )
        await bot_app.process_update(update)


def _extract_update(event):
    """Returns (telegram_update_dict, secret_token_header_or_None).

    Supports both the Lambda Function URL / HTTP API envelope ({"body": ...,
    "headers": ...}) and the legacy REST API non-proxy integration, which
    passes the raw Telegram update as the event itself.
    """
    if "body" not in event:
        return event, None
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body), headers.get("x-telegram-bot-api-secret-token")


_lambda_client = None


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _process(update):
    try:
        asyncio.run(tg_bot_main(app, update))
    except Exception:
        # A failed update may have completed side effects; do not request replay.
        logger.exception("Failed to process update %s", update.get("update_id"))


def _handoff(update, context):
    """Re-invokes this function asynchronously with the update. Returns False
    if that fails so the caller can fall back to processing inline."""
    try:
        _lambda().invoke(
            FunctionName=context.invoked_function_arn,
            InvocationType="Event",
            Payload=json.dumps({ASYNC_KEY: update}),
        )
        return True
    except Exception:
        logger.exception("Async handoff failed for update %s; processing inline", update.get("update_id"))
        return False


def lambda_handler(event, context):
    if "healthCheck" in event:
        return {"statusCode": 200}
    if ASYNC_KEY in event:
        # Second hop of the handoff: do the actual work.
        _process(event[ASYNC_KEY])
        return {"statusCode": 200}
    try:
        update, token = _extract_update(event)
    except (ValueError, UnicodeDecodeError):
        logger.warning("Rejected request with unparseable body")
        return {"statusCode": 400}
    if WEBHOOK_SECRET and not hmac.compare_digest(token or "", WEBHOOK_SECRET):
        logger.warning("Rejected request with missing/invalid webhook secret")
        return {"statusCode": 401}
    if not isinstance(update, dict) or "update_id" not in update:
        return {"statusCode": 400}
    # Only Function URL requests need the handoff; the legacy REST API already
    # invokes asynchronously (its event has no "body" envelope).
    if ASYNC_HANDOFF and "body" in event and context is not None and _handoff(update, context):
        return {"statusCode": 200}
    _process(update)
    return {"statusCode": 200}


# Exclude edited messages; route unknown commands separately from searches.
MSG = filters.UpdateType.MESSAGE
app.add_handler(CommandHandler("start", start, filters=MSG))
app.add_handler(MessageHandler(filters.CONTACT & MSG, contact))
app.add_handler(CommandHandler("help", help_command, filters=MSG))
app.add_handler(CommandHandler("leave", leave, filters=MSG))
app.add_handler(MessageHandler(filters.COMMAND & MSG, unknown_command))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & MSG, search))
app.add_handler(CallbackQueryHandler(answer_callback))
app.add_error_handler(on_error)

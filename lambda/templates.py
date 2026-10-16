start = """
<b>Registration is required before using this bot.</b>

In order to prevent the distribution of copyright material, your phone number will be collected for automated user verification. It will never be shared or used for any other purpose.
"""

allowed_phone = ("65", "61", "60")

current_version = 4

changelog = """
<b>Welcome back to the TSMS bot!</b>

We've made some changes since your last use. This latest version includes:

- General improvements including fixing typo errors across all books
- Added full lyrics for Hymns of Grace (HOG)
- Introducing <i>Communities</i>, a new feature for group singspiration. Search for the same song with 3 or more people to try it out!
"""

welcome = """
<b>Welcome to the TSMS bot!</b>

<i>Let the word of Christ dwell in you richly in all wisdom; teaching and admonishing one another in psalms and hymns and spiritual songs, singing with grace in your hearts to the Lord.
Colossians 3:16</i>

Send me a <b>song number, title or lyrics</b>, or type /help for instructions.
"""

community_disabled = "Communities are currently unavailable."

community_join = """
⛪️ <b>You have joined a new Community!</b> ⛪️

Others are also searching for <b>{title}</b>! We have automatically formed a <i>Community</i>, making it easy to share songs with everyone.

You'll see a <i>"Send to Community"</i> option when you search for songs, and will receive songs sent by people in the same <i>Community</i>.

Use /leave to exit the <i>Community</i>. Communities automatically expire after a period of inactivity.
"""

group_not_in_community = "<i>You are currently not in a Community</i>"

group_left = "<i>You have left the Community</i>"

group_sent_caption = "\n\n<i>Sent by {name}. Use /leave to exit the Community.</i>"

group_send_expired = "This Community has ended"

group_send_already = "Someone in your Community already shared this song."

group_send_nobody = "No one else is in your Community right now."

group_closed = "This Community has ended because no other members remain."

group_send_done = "Sent to {n} member{s}"
group_send_button = "📢 Send to Community"
group_send_sending = "📤 Sending to Community…"
group_send_sent = "✅ Sent to {n} member{s}"

# Stale or invalid callback buttons.
callback_invalid = "That button is from an older version. Search the song again to get fresh buttons."
callback_missing = "This song is no longer available. Search again to see the current version."

not_registered = "You're not registered yet. Send /start and tap REGISTER to get started."

unknown_command = (
    "Unknown command. Send a song number, title or line of lyrics to search, "
    "or /help for instructions."
)

generic_error = "Something went wrong. Please try again in a moment."

video_toast = "Sending {n} video{s}…"
explain_toast = "Thinking..."

examples = """
<b>HOW TO USE</b>

Search by typing any of the following:
- <b>Song number</b>, e.g. <i>1</i> (just a number searches TSMS)
- <b>Book & number</b>, e.g. <i>HGG 123</i>
- <b>Song title</b>, e.g. <i>trust and obey</i>
- <b>Song lyrics</b>, e.g. <i>for there's no other way</i>

<u>Available Books</u>
<b>Then Sings My Soul</b> (TSMS 1-768)
<b>TSMS Chinese Songs</b> (C 1-32)
<b>Hymns of Grace & Glory</b> (HGG 1-740)
<b>Revival Hymns & Choruses</b> (RHC 1-567)
<b>Hymns of Grace</b> (HOG 1-444)
<b>The Wilds Songbook 9th ed.</b> (WILDS 1-209)
<b>Children's Ministry</b> (CM 1-80)
<b>Word in Song - Bukit Arang Church</b> (WIS)
<b>CityAlight</b> (CA)
<b>Getty Music</b> (G)
<b>Sovereign Grace Music</b> (SGM)
"""

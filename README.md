# TSMS Bot (v2.0)

A serverless Telegram bot for church members to search for song lyrics, musical scores and audio recordings across a catalog of over 3,700 songs and several hymnbooks.

## Key Capabilities

- **Search & Lookups**: Instant search by songbook number or fuzzy lyric matching across editions.
- **Worship Media**: On-demand delivery of sheet music scores, piano/choral MP3s, chords, and choir video links.
- **PowerPoint Generation**: Generates projection-ready `.pptx` lyric slides on the fly.
- **AI Song Explanations**: Devotional insights and theological context powered by Google Gemini.
- **Communities**: Real-time co-worship groups formed when users browse songs together, featuring song broadcasting, invitation windows, and auto-rollover.

## Architecture & Stack

- **Compute**: AWS Lambda running Python 3.14 on `arm64`, exposed via Lambda Function URLs with asynchronous self-invocation for Telegram webhooks.
- **Storage**: AWS DynamoDB for user state, live Community sessions, search recents, and audit logs.
- **Monitoring**: CloudWatch alarms with an isolated notification Lambda, retry backoff, and an encrypted dead-letter queue.
- **CI/CD**: GitHub Actions using AWS OIDC authentication — auto-deploying to staging on push to `main` and manually dispatched to production.
- **Testing**: Comprehensive automated suite (`pytest tests`) with mocked AWS and Telegram services.
# newtsmsbot
 TSMS Bot v2.0

Community matching reuses the generated title lookup. All editions under the
same normalized title share a Community identity, regardless of lyric differences.
There are no manual equivalence exceptions. Different title wordings remain
separate, following the existing search lookup. Regenerate the lookup with
`python3 utilities/cacheloader.py` after changing the song catalog.
Messages, resources and log requests retain the selected edition. Formation,
invitations and duplicate-broadcast protection use the shared identity, with no
additional database calls.

Alarm emails run in a separate notifier Lambda, so bot initialization failures
do not prevent notification delivery. The notifier retries failed publications
twice after the initial invocation. Exhausted or expired notifications enter an
encrypted SQS dead-letter queue retained for 14 days; its URL is exported as
`AlertFailuresQueueUrl`. After fixing delivery, replay the original SNS event
from the queue to the notifier and remove it only after confirming publication.
Queue recovery is manual. Bot update retries remain disabled to avoid replaying
Telegram side effects. Existing GitHub deployment roles need the updated
`infra/github-oidc.yaml` permissions before deploying these additional resources.

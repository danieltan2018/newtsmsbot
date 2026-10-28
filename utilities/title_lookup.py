"""Build search titles and Community identities from the same title groups."""
import re
from collections import defaultdict

from unidecode import unidecode


def title_key(title):
    return re.sub("[^a-zA-Z ]", "", unidecode(title).strip().upper())


def _edition_order(song):
    book, number = song.split()
    return (book != "TSMS", book, int(number))


def build_title_lookup(titles):
    lookup = defaultdict(list)
    for song, title in titles.items():
        candidates = lookup[title_key(title)]
        if song.startswith("TSMS "):
            candidates.insert(0, song)
        else:
            candidates.append(song)

    community_keys = {}
    for editions in lookup.values():
        if len(editions) > 1:
            canonical = min(editions, key=_edition_order)
            community_keys.update((song, canonical) for song in editions)
    return dict(lookup), community_keys

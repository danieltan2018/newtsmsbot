"""Community matching follows the existing normalized title groups."""
from cache import TITLES
from lookup import COMMUNITY_SONG_KEYS, TITLES_LOOKUP
from utilities.title_lookup import build_title_lookup


def test_same_normalized_title_shares_a_community_identity():
    titles = {"TSMS 1": "Praise Him!", "HGG 2": "PRAISE HIM"}
    lookup, keys = build_title_lookup(titles)
    assert lookup["PRAISE HIM"] == ["TSMS 1", "HGG 2"]
    assert keys == {"TSMS 1": "TSMS 1", "HGG 2": "TSMS 1"}


def test_different_titles_remain_separate():
    titles = {"HGG 2": "Long Title", "TSMS 1": "Short Title", "RHC 3": "Long Title"}
    lookup, keys = build_title_lookup(titles)
    assert lookup["SHORT TITLE"] == ["TSMS 1"]
    assert lookup["LONG TITLE"] == ["HGG 2", "RHC 3"]
    assert keys == {"HGG 2": "HGG 2", "RHC 3": "HGG 2"}


def test_community_identity_is_stable_when_catalog_order_changes():
    titles = {"HGG 3": "Song", "TSMS 2": "Song", "TSMS 1": "Song"}
    _, keys = build_title_lookup(titles)
    _, reversed_keys = build_title_lookup(dict(reversed(list(titles.items()))))
    assert keys == reversed_keys == dict.fromkeys(titles, "TSMS 1")


def test_generated_lookup_matches_the_catalog():
    lookup, keys = build_title_lookup(TITLES)
    assert lookup == TITLES_LOOKUP
    assert keys == COMMUNITY_SONG_KEYS
    for editions in lookup.values():
        identities = {keys.get(song, song) for song in editions}
        assert len(identities) == 1
    assert keys["TSMS 24"] == keys["HGG 523"]
    assert keys["TSMS 579"] == keys["HGG 500"]
    assert keys["TSMS 607"] == keys["TSMS 652"]
    assert lookup["WE PLOUGH THE FIELDS"] != lookup["WE PLOUGH THE FIELDS AND SCATTER"]

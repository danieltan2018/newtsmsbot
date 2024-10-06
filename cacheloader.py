import json
import os
import re
from collections import defaultdict

from unidecode import unidecode

f = open("./lambda/cache.py", "w", encoding="UTF-8")

global songs
songs = {}
global titles
titles = {}
number = re.compile("^\d+ ")
for filename in (x.name for x in os.scandir("./books") if x.is_file()):
    filepath = "./books/" + filename
    with open(filepath, "r", encoding="UTF8") as book:
        filename = filename.split(".")
        bookname = filename[0]
        print("Loading", bookname)
        song_number = None
        song_lyrics = ""
        for line in book:
            if number.search(line):
                if song_number != None:
                    song_lyrics = song_lyrics.strip()
                    songs[song_number] = song_lyrics
                    song_lyrics = ""
                song_lyrics += bookname + " " + line
                line = line.split()
                song_number = bookname + " " + str(line.pop(0))
                song_title = " ".join(line)
                titles[song_number] = song_title
            else:
                song_lyrics += line
        song_lyrics = song_lyrics.strip()
        songs[song_number] = song_lyrics

f.write("titles = " + json.dumps(titles, ensure_ascii=False) + "\n")
f.write("songs = " + json.dumps(songs, ensure_ascii=False) + "\n")

global chords
chords = {}
number = re.compile("^\d")
with open("./media/tsms_chords.txt", "r", encoding="UTF8") as tsms_chords_file:
    print("Loading Chords")
    song_number = None
    song_lyrics = ""
    for line in tsms_chords_file:
        if number.search(line):
            if song_number != None:
                song_lyrics = song_lyrics.strip()
                chords[song_number] = song_lyrics
                song_lyrics = ""
            song_lyrics += "TSMS " + line
            line = line.split()
            song_number = "TSMS " + str(line.pop(0))
        else:
            song_lyrics += line
    song_lyrics = song_lyrics.strip()
    chords[song_number] = song_lyrics

with open("./media/cm_chords.txt", "r", encoding="UTF8") as cm_chords_file:
    print("Loading CM Chords")
    song_number = None
    song_lyrics = ""
    for line in cm_chords_file:
        if number.search(line):
            if song_number != None:
                song_lyrics = song_lyrics.strip()
                chords[song_number] = song_lyrics
                song_lyrics = ""
            song_lyrics += "CM " + line
            line = line.split()
            song_number = "CM " + str(line.pop(0))
        else:
            song_lyrics += line
    song_lyrics = song_lyrics.strip()
    chords[song_number] = song_lyrics

f.write("chords = " + json.dumps(chords, ensure_ascii=False) + "\n")

global scores
scores = {}
with open("./media/scores.txt", "r", encoding="UTF8") as scores_file:
    print("Loading Scores")
    for line in scores_file:
        line = line.strip()
        line = line.split("@")
        reference = line[1]
        line = line[0]
        line = line.split("_")
        number = line[0]
        scores.setdefault(number, []).append(reference)

f.write("scores = " + json.dumps(scores, ensure_ascii=False) + "\n")

global mp3
mp3 = {}
with open("./media/mp3.txt", "r", encoding="UTF8") as mp3_file:
    print("Loading MP3")
    for line in mp3_file:
        line = line.strip()
        line = line.split("@")
        reference = line[1]
        line = line[0]
        line = line.split("_")
        number = line[0]
        mp3.setdefault(number, []).append(reference)

f.write("mp3 = " + json.dumps(mp3, ensure_ascii=False) + "\n")

global piano
piano = {}
with open("./media/wilds_piano.txt", "r", encoding="UTF8") as piano_file:
    print("Loading Piano")
    for line in piano_file:
        line = line.strip()
        line = line.split("@")
        reference = line[1]
        number = line[0]
        piano[number] = reference

f.write("piano = " + json.dumps(piano, ensure_ascii=False) + "\n")

global ca_links
ca_links = {}
with open("./media/ca_links.txt", "r", encoding="UTF8") as ca_links_file:
    print("Loading CA Links")
    song_number = None
    links = {}
    for line in ca_links_file:
        line = line.strip()
        if not line and song_number != None:
            ca_links[song_number] = links
            song_number = None
            links = {}
        elif line.isnumeric():
            song_number = "CA " + line
        else:
            line = line.split("|")
            href = line[1]
            text = line[0]
            links[text] = href

f.write("ca_links = " + json.dumps(ca_links, ensure_ascii=False) + "\n")

global sgm_links
sgm_links = {}
with open("./media/sgm_links.txt", "r", encoding="UTF8") as sgm_links_file:
    print("Loading SGM Links")
    song_number = None
    links = {}
    for line in sgm_links_file:
        line = line.strip()
        if not line and song_number != None:
            sgm_links[song_number] = links
            song_number = None
            links = {}
        elif line.isnumeric():
            song_number = "SGM " + line
        else:
            line = line.split("|")
            href = line[1]
            text = line[0]
            links[text] = href

f.write("sgm_links = " + json.dumps(sgm_links, ensure_ascii=False) + "\n")

d = open("./lambda/lookup.py", "w", encoding="UTF-8")
alpha = re.compile("[^a-zA-Z ]")
titles_decoded = defaultdict(list)
songs_decoded = {}
for song_number, song_title in titles.items():
    title = unidecode(song_title).strip().upper()
    title = alpha.sub("", title)
    if song_number.startswith("TSMS"):
        titles_decoded[title].insert(0, song_number)
    else:
        titles_decoded[title].append(song_number)
for song_number, song_lyrics in songs.items():
    lyrics = unidecode(song_lyrics).replace("\n", " ").strip().upper()
    lyrics = alpha.sub("", lyrics)
    songs_decoded[song_number] = lyrics

d.write("titles_lookup = " + json.dumps(titles_decoded, ensure_ascii=False) + "\n")
d.write("songs_lookup = " + json.dumps(songs_decoded, ensure_ascii=False) + "\n")

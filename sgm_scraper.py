import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from unidecode import unidecode

base_url = "https://sovereigngracemusic.com/music/songs/"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}


song_links = {}


def get_song_links(url):
    print(url)
    req = requests.get(url, headers=headers)
    soup = BeautifulSoup(req.content, "html.parser")
    titles = soup.find_all("h3")
    for title in titles:
        link = title.find("a", href=True)
        if link:
            link = link.attrs.get("href")
            if link.startswith(base_url):
                song_links[link.replace(base_url, "")] = {}
    next_page = soup.find("a", class_="page-numbers next")
    if next_page:
        return next_page.attrs.get("href")
    return None


url = base_url
while url:
    url = get_song_links(url)


def populate_song_content(song):
    print(song)
    req = requests.get(base_url + song, headers=headers)
    soup = BeautifulSoup(req.content, "html.parser")
    song_links[song] = soup


with ThreadPoolExecutor(max_workers=50) as executor:
    for song in song_links.keys():
        executor.submit(populate_song_content, song)


book = open("./books/SGM.txt", "w+")
media = open("./media/sgm_links.txt", "w+")

counter = 1

for song, soup in song_links.items():
    base_title = unidecode(soup.find("h1").text)
    title = str(counter) + " " + base_title.upper()
    print(title)
    lyrics_box = soup.find("div", class_="elementor-widget-theme-post-content")
    if not lyrics_box:
        continue
    lyrics_stanzas = lyrics_box.find_all("p")
    lyrics = title
    for stanza in lyrics_stanzas:
        lyrics += "\n\n" + stanza.text
    links = str(counter)
    resources_box = soup.find("div", class_="song_resources")
    resources = resources_box.find_all("a", href=True)
    for link in resources:
        if link.text and link.attrs.get("href"):
            links += "\n" + link.text + "|" + link.attrs.get("href")
    buy_box = soup.find("div", class_="song_listen-buy")
    buys = buy_box.find_all("a", href=True)
    for link in buys:
        if link.text and link.attrs.get("href"):
            href = link.attrs.get("href")
            if "open.spotify.com" in href:
                links += "\n" + "Spotify|" + href
    video_box = soup.find("div", class_="glide__track")
    if video_box:
        videos = video_box.find_all("iframe")
        for link in videos:
            video_url = re.search(r"embed\/(.+)\?", link.attrs.get("src"))
            video_id = video_url.group(1)
            links += (
                "\n"
                + "YouTube: "
                + unidecode(link.attrs.get("title")).lstrip(base_title).strip("* []")
                + "|"
                + "https://www.youtube.com/watch?v="
                + video_id
            )
    book.write(unidecode(lyrics))
    book.write("\n\n")
    media.write(unidecode(links))
    media.write("\n\n")
    counter += 1

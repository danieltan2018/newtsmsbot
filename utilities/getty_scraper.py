import requests
from bs4 import BeautifulSoup
from unidecode import unidecode

base_url = "https://gettymusic.store"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
}

SONG_LINKS = []
ALL_LYRICS = set()


def get_song_links(url):
    req = requests.get(url, headers=headers)
    soup = BeautifulSoup(req.content, "html.parser")
    titles = soup.find_all("div", {"class": "songs-product-box"})
    for title in titles:
        link = title.find("a", href=True)
        if link:
            link = link.attrs.get("href")
            SONG_LINKS.append(link)
    next_page = soup.find("a", class_="next page-numbers", href=True)
    if next_page:
        return next_page.attrs.get("href")
    return None


url = "/collections/all-songs?page=1&sort_by=created-ascending"
while url:
    url = get_song_links(base_url + url)

book = open("./books/G.txt", "w+")
counter = 1
for url in SONG_LINKS:
    req = requests.get(base_url + url, headers=headers)
    soup = BeautifulSoup(req.content, "html.parser")
    title = soup.find("h1", {"class": "product-title"}).text
    title = unidecode(title).upper().strip()
    print(title)
    lyrics_box = soup.find("div", {"class": "song-lyrics"}).find(
        "div", {"class": "content-container"}
    )
    for p in lyrics_box.find_all(["p"]):
        p.append("\n\n")
    for br in lyrics_box.select("br"):
        br.replace_with("\n")
    lyrics = unidecode(lyrics_box.text).strip()
    if "\n" not in lyrics:
        print(f"===== Skipping empty lyrics for {title} =====")
        continue
    if lyrics in ALL_LYRICS:
        print(f"===== Skipping duplicate for {title} =====")
        continue
    ALL_LYRICS.add(lyrics)
    book.write(str(counter) + " " + title + "\n\n" + lyrics + "\n\n")
    counter += 1

book.close()
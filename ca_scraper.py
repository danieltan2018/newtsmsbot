from bs4 import BeautifulSoup, SoupStrainer
import requests
from unidecode import unidecode

baseurl = "https://cityalight.com"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}

url = baseurl + "/resources/"
req = requests.get(url, headers=headers)
soup = BeautifulSoup(req.content, "html.parser")
soup = soup.find_all("div", class_="album")
soup.reverse()

songs = []

for album in soup:
    for link in album.find_all("a", href=True):
        link = link.attrs.get("href").replace(baseurl, "")
        if link.startswith("/song/") and link not in songs:
            songs.append(link)

counter = 1

book = open("./books/CA.txt", "w+")
media = open("./media/ca_links.txt", "w+")

for song in songs:
    url = baseurl + song
    req = requests.get(url, headers=headers)
    soup = BeautifulSoup(req.content, "html.parser")

    number = str(counter)
    title = unidecode(soup.find("div", class_="album-title").text.strip())
    print(title)

    book.write(number + " " + title.upper())
    book.write("\n")
    lyrics = soup.select_one(
        '[class^="et_pb_module et_pb_text et_pb_text_7"]'
    ) or soup.select_one('[class^="et_pb_module et_pb_text et_pb_text_6"]')
    lyrics = lyrics.get_text(separator="\n")
    lyrics = lyrics.replace("\n\n", "\n")
    lyrics = lyrics.replace("Æ", "'")
    lyrics = lyrics.replace("æ", "'")
    book.write(unidecode(lyrics))
    book.write("\n")

    media.write(number)
    media.write("\n")
    links = soup.find("div", class_="album")
    for link in links.find_all("a", href=True):
        if link.text and link.attrs.get("href") != "#":
            media.write(unidecode(link.text))
            media.write("|")
            media.write(link.attrs.get("href"))
            media.write("\n")
    media.write("\n")

    counter += 1

book.close()
media.close()

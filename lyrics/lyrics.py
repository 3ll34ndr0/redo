import lyricsgenius
import os
import re

# --- CONFIGURATION ---
TOKEN = os.getenv("GENIUS_ACCESS_TOKEN")
print(os.getenv("GENIUS_ACCESS_TOKEN"))
ARTIST_NAME = "Patricio Rey y sus Redonditos de Ricota"
OUTPUT_DIR = "./corpus"

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)
print(f"{TOKEN}")
genius = lyricsgenius.Genius(TOKEN)

# Removes non-lyrical text like [Intro], [Solo], and the Genius "Embed" tags
def clean_lyrics(text):
    # Remove headers like [Estribillo], [Solo de saxo], etc.
    text = re.sub(r'\[.*?\]', '', text)
    # Remove Genius artifacts like '23Embed' or 'You might also like'
    text = re.sub(r'\d*Embed$', '', text)
    text = re.sub(r'.*?Lyrics', '', text, count=1) # Remove "Song Title Lyrics" header
    return text.strip()

def download_discography():
    print(f"Searching for {ARTIST_NAME}...")
    artist = genius.search_artist(ARTIST_NAME, sort="title")
    
    for song in artist.songs:
        print(f"Processing: {song.title}")
        
        # Sanitize filename (remove characters MFA doesn't like)
        filename = re.sub(r'[^\w\s-]', '', song.title).strip().replace(' ', '_').lower()
        filepath = os.path.join(OUTPUT_DIR, f"{filename}.txt")
        
        cleaned_text = clean_lyrics(song.lyrics)
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(cleaned_text)

if __name__ == "__main__":
    download_discography()

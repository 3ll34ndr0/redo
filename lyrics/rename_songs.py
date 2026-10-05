import os
import re

ORIGINAL_DIR=os.getenv("ORIGINAL_DIR", "/home/lean/Descargas/320/")
NEW_DIR=os.getenv("MUSIC_PATH", "./music")
print(NEW_DIR)
disks = [x[0] for x in os.walk(ORIGINAL_DIR)]



import re

def sanitize_filename(song):
    titulos = ["Los Redonditos de Ricota", "Patricio Rey & Sus Redonditos De Ricota",
              "Patricio Rey y sus redonditos", "La mosca y la sopa"]
    symbols = [r"^\d{1,}", "-", r"[^\w\s-]"]
    regexes = titulos + symbols
    for cadena in regexes:
        song = re.sub(f"{cadena}", "", song)
    filename = song.strip().replace(" ", "_").lower()
    filename = re.sub(r'_{2,}', "_", filename) # Double underscores to one
    filename = re.sub(r"_?mp3$", ".mp3", filename)
    return filename

for disk in disks[1:]:
    songs = os.listdir(disk)
    for song in songs:
        if song.endswith('mp3'):
            filename = sanitize_filename(song)
#            print(ORIGINAL_DIR, os.path.basename(disk),song)
#            print(os.path.join(NEW_DIR, filename))
            os.link(os.path.join(ORIGINAL_DIR, os.path.basename(disk),song), os.path.join(NEW_DIR, filename))





#        # Sanitize filename (remove characters MFA doesn't like)
#       filepath = os.path.join(OUTPUT_DIR, f"{filename}.txt")
#       
#       cleaned_text = clean_lyrics(song.lyrics)
#       
#       with open(filepath, "w", encoding="utf-8") as f:
#           f.write(cleaned_text)



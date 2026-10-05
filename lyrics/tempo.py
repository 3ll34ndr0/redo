import os
import sqlite3
from praatio import textgrid

# Configuration
TEXTGRID_DIR = "./"
DB_PATH = "redondos_master.db"
MAX_WINDOW = 6 

def build_ricotero_index():
    # Connect and setup DB
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("DROP TABLE IF EXISTS redondos_search")
    cursor.execute("""
        CREATE VIRTUAL TABLE redondos_search USING fts5(
            song, 
            lyric, 
            start UNINDEXED, 
            end UNINDEXED
        )
    """)

    for filename in os.listdir(TEXTGRID_DIR):
        if not filename.endswith(".TextGrid"):
            continue
            
        tg_path = os.path.join(TEXTGRID_DIR, filename)
        
        # Adding the required positional argument: False (for includeEmptyIntervals)
        tg = textgrid.openTextgrid(tg_path, False)
        
        # Get the word tier
        tier_name = 'words' 
        if tier_name not in tg.tierNames:
            tier_name = tg.tierNames[0]
            
        tier = tg.getTier(tier_name)
        
        # Filter out empty labels just in case
        entries = [e for e in tier.entries if e.label.strip()]
        
        song_name = os.path.splitext(filename)[0].replace("_vocals", "")

        for size in range(1, MAX_WINDOW + 1):
            for i in range(len(entries) - size + 1):
                window = entries[i : i + size]
                
                phrase_text = " ".join([e.label.lower() for e in window]).strip()
                
                # Start and end attributes
                phrase_start = window[0].start
                phrase_end = window[-1].end
                
                if phrase_text:
                    cursor.execute("INSERT INTO redondos_search VALUES (?, ?, ?, ?)", 
                                   (song_name, phrase_text, phrase_start, phrase_end))
    
    conn.commit()
    conn.close()
    print(f"Index built! Windows 1-{MAX_WINDOW} ready for searching.")

if __name__ == "__main__":
    build_ricotero_index()

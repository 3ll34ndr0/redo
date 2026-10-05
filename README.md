# redo
Text to audio cut service

* First stage: Get the lyrics corpus
* Download the songs and normalize their names 
* Split voice with Demucs
* Align lyrics with songs (using only voice track) to create the timestamp database for the lyrics
* Glue the lyrics search with the audio cut tool (ffmpeg) to produce the song snippet


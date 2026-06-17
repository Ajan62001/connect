"""Standalone 'sing the news' experiment — generate one news SONG via ElevenLabs
Music and save the mp3 so you can hear it.

Run from backend/ (loads backend/.env):

    python scripts/music_experiment.py                 # default RBI news song
    python scripts/music_experiment.py --out /tmp/song.mp3

It builds a composition_plan (the vocals sing these exact lyrics) and writes the
generated mp3. Spends ElevenLabs credits.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connect.orchestration.config import Settings  # noqa: E402
from connect.social import music  # noqa: E402

# a tiny, catchy, factual news song (the running RBI example)
_SECTIONS = [
    ("Verse 1", ["The Reserve Bank held the rate today",
                 "Six point five, it's here to stay"]),
    ("Chorus", ["R-B-I holds the line, holds the line",
                "Inflation's cooling down, doing fine"]),
    ("Verse 2", ["Markets in Mumbai closing green",
                 "Steadiest hand the street has seen"]),
    ("Outro", ["That's your money news — from connect"]),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Sing the news (ElevenLabs Music)")
    ap.add_argument("--out", default="/tmp/news_song.mp3")
    args = ap.parse_args()

    settings = Settings()
    print("Music configured (ElevenLabs key):", music.is_configured(settings))
    if not music.is_configured(settings):
        print("Set ELEVENLABS_API_KEY in backend/.env first.")
        return 1

    plan = music.build_news_plan(_SECTIONS)
    print(f"Composing a {len(plan['sections'])}-section news song "
          f"(this can take ~30-90s)...")
    try:
        mp3 = music.compose_song(settings=settings, composition_plan=plan)
    except music.MusicError as e:
        print(f"\nFailed: {e}")
        return 1
    out = Path(args.out)
    out.write_bytes(mp3)
    print(f"\nWrote {len(mp3):,} bytes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

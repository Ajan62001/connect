"""Standalone HeyGen presenter experiment — verify the API + render one clip.

Run from the backend/ directory (loads backend/.env automatically):

    python scripts/heygen_experiment.py                 # list avatars + voices
    python scripts/heygen_experiment.py --render         # + render a test clip
    python scripts/heygen_experiment.py --render \\
        --text "RBI holds the repo rate at 6.5 percent for the sixth time."

It prints the configured avatar/voice, lists a few of the account's avatars and
voices (so you can pick IDs for backend/.env), and — with --render — generates a
short vertical talking-head MP4 and writes it to /tmp so you can watch it before
wiring presenter mode into the reel pipeline.

This calls the real HeyGen API and spends credits only when --render is passed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# allow running as a plain script (python scripts/heygen_experiment.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connect.orchestration.config import Settings  # noqa: E402
from connect.social import heygen  # noqa: E402

_DEFAULT_TEXT = (
    "Good evening. Here is your India business briefing. The Reserve Bank of "
    "India held the repo rate steady at six point five percent today, citing "
    "easing inflation and steady growth.")


def _show(label: str, rows: list[dict], fields: list[str], limit: int) -> None:
    print(f"\n{label} ({len(rows)} total, showing {min(limit, len(rows))}):")
    if not rows:
        print("  (none — check the API key)")
        return
    for r in rows[:limit]:
        print("  " + "  ".join(f"{f}={r.get(f)!r}" for f in fields))


def main() -> int:
    ap = argparse.ArgumentParser(description="HeyGen presenter experiment")
    ap.add_argument("--render", action="store_true",
                    help="actually render a test clip (spends credits)")
    ap.add_argument("--text", default=_DEFAULT_TEXT, help="script to speak")
    ap.add_argument("--limit", type=int, default=12,
                    help="how many avatars/voices to list")
    ap.add_argument("--out", default="/tmp/heygen_test.mp4",
                    help="where to write the rendered clip")
    args = ap.parse_args()

    settings = Settings()
    print("HeyGen configured:", heygen.is_configured(settings))
    print("  api_key:", "set" if settings.heygen_api_key else "MISSING")
    print("  avatar_id:", settings.heygen_avatar_id or "(unset)")
    print("  voice_id:", settings.heygen_voice_id or "(unset)")
    print("  avatar_style:", settings.heygen_avatar_style)
    print("  background:", settings.heygen_background)

    if not settings.heygen_api_key:
        print("\nSet HEYGEN_API_KEY in backend/.env first.")
        return 1

    _show("Avatars", heygen.list_avatars(settings),
          ["id", "name", "gender"], args.limit)
    _show("Voices", heygen.list_voices(settings),
          ["id", "name", "language", "gender"], args.limit)

    if not args.render:
        print("\n(dry run — pass --render to generate a clip)")
        return 0

    if not settings.heygen_avatar_id:
        print("\nPick an avatar id above and set CONNECT_HEYGEN_AVATAR_ID in "
              "backend/.env before rendering.")
        return 1

    print(f"\nRendering (this can take 1-3 min)...\n  text: {args.text!r}")
    try:
        mp4 = heygen.generate_avatar_clip(args.text, settings=settings)
    except heygen.HeyGenError as e:
        print(f"\nRender failed: {e}")
        return 1
    out = Path(args.out)
    out.write_bytes(mp4)
    print(f"\nWrote {len(mp4):,} bytes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

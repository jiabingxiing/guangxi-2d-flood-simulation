"""Inspect asset PNG dimensions/sizes across processed and external dirs."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(__file__).resolve().parents[1]
DIRS = (
    ROOT / "data" / "processed" / "assets",
    ROOT / "data" / "processed" / "optical",
    ROOT / "data" / "external" / "optical",
)


def main() -> int:
    found = 0
    for base in DIRS:
        if not base.is_dir():
            print(f"(missing) {base.relative_to(ROOT)}")
            continue
        for path in sorted(base.rglob("*.png")):
            found += 1
            try:
                with Image.open(path) as im:
                    width, height = im.size
                    mode = im.mode
            except Exception as exc:
                print(f"ERR {path.relative_to(ROOT)}: {exc}")
                continue
            mb = path.stat().st_size / 1e6
            flag = "OK " if max(width, height) <= 4096 else "BIG"
            print(f"{flag} {path.relative_to(ROOT).as_posix():52s} {width}x{height} {mode} {mb:.2f} MB")
    print(f"[inspect_assets] total {found} png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

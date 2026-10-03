from pathlib import Path
import base64

root = Path(__file__).resolve().parent
for src in (root / "attachments").glob("*.png.base64"):
    dst = src.with_suffix("")
    dst.write_bytes(base64.b64decode(src.read_text(encoding="utf-8").strip()))
    print(f"decoded {src.name} -> {dst.name}")

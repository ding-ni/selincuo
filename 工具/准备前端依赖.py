"""下载固定版本浏览器依赖至独立目录，已有文件仅核验。"""
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "vue.global.prod.js": "https://cdn.jsdelivr.net/npm/vue@3.5.13/dist/vue.global.prod.js",
    "maplibre-gl.js": "https://cdn.jsdelivr.net/npm/maplibre-gl@5.3.0/dist/maplibre-gl.js",
    "maplibre-gl.css": "https://cdn.jsdelivr.net/npm/maplibre-gl@5.3.0/dist/maplibre-gl.css",
    "lucide.js": "https://unpkg.com/lucide@0.468.0/dist/umd/lucide.js",
    "vue-LICENSE": "https://unpkg.com/vue@3.5.13/LICENSE",
    "maplibre-LICENSE": "https://unpkg.com/maplibre-gl@5.3.0/LICENSE.txt",
    "lucide-LICENSE": "https://unpkg.com/lucide@0.468.0/LICENSE",
}


def main():
    target = ROOT / "前端/依赖"
    target.mkdir(exist_ok=True)
    manifest = ROOT / "部署/前端依赖锁定.json"
    expected = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else None
    records = []
    for name, url in FILES.items():
        path = target / name
        content = path.read_bytes() if path.exists() else urlopen(url, timeout=60).read()
        sha = hashlib.sha256(content).hexdigest()
        item = {"name": name, "url": url, "sha256": sha, "bytes": len(content)}
        if expected is not None and item not in expected:
            raise ValueError("依赖与锁定清单不一致：" + name)
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(content)
        records.append(item)
        print(name, len(content), sha)
    if expected is None:
        with manifest.open("x", encoding="utf-8") as stream:
            json.dump(records, stream, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()

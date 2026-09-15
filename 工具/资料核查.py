from pathlib import Path
import csv
import json
import hashlib
import fitz
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '文档' / '核查记录'
OUT.mkdir(parents=True, exist_ok=True)
SOURCE = Path('F:/新安江水文模型')
EXCLUDE = {'.git', 'node_modules', '__pycache__', 'dist', '前端重构备份', '前端重构恢复验证', '归档', 'test-results', '测试结果', 'test-output'}

def walk(folder):
    for p in sorted(folder.iterdir()):
        if p.is_symlink() or p.name in EXCLUDE:
            continue
        if p.is_dir():
            yield from walk(p)
        elif p.is_file():
            yield p

rows = [{'相对路径': str(p.relative_to(SOURCE)), '字节数': p.stat().st_size, '扩展名': p.suffix.lower()} for p in walk(SOURCE)]
with (OUT / '旧项目文件索引.csv').open('x', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['相对路径', '字节数', '扩展名'])
    w.writeheader()
    w.writerows(rows)

doc = fitz.open(ROOT / '任务书最终盖章签字版本.pdf')
with (OUT / '任务书逐页提取.txt').open('x', encoding='utf-8') as f:
    for i, p in enumerate(doc):
        f.write(f'\n=== PDF物理页 {i+1} ===\n{p.get_text()}')
pages = [1, 6, 7, 8, 9, 10, 11, 12, 14, 33, 34, 36, 47, 48, 49, 50]
for group in range(4):
    sheet = Image.new('RGB', (1400, 2000), 'white')
    draw = ImageDraw.Draw(sheet)
    for j, n in enumerate(pages[group*4:group*4+4]):
        pix = doc[n-1].get_pixmap(matrix=fitz.Matrix(1.4, 1.4))
        im = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
        im.thumbnail((690, 950))
        x, y = (j % 2)*700, (j//2)*1000
        sheet.paste(im, (x, y+25))
        draw.text((x+10, y+5), f'PDF PAGE {n}', fill='black')
    sheet.save(OUT / f'任务书核查图_{group+1}.png')
print(json.dumps({'indexed_files': len(rows), 'pdf_pages': len(doc)}, ensure_ascii=False))

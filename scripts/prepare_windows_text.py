"""Windows PowerShell 5 requires a BOM to decode Chinese script literals."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for folder in ('installer', 'scripts'):
    for path in (ROOT / folder).glob('*.ps1'):
        text = path.read_text(encoding='utf-8-sig')
        path.write_text(text, encoding='utf-8-sig')
print('PowerShell scripts saved as UTF-8 with BOM.')

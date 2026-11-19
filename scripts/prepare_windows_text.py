"""Windows PowerShell 5 requires a BOM to decode Chinese script literals."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for folder in ('installer', 'scripts'):
    for path in (ROOT / folder).glob('*.ps1'):
        contents = path.read_bytes()
        if not contents.startswith(b'\xef\xbb\xbf'):
            contents.decode('utf-8')
            path.write_bytes(b'\xef\xbb\xbf'+contents)
print('PowerShell scripts saved as UTF-8 with BOM.')

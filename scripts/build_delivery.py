"""Build the reviewed Git source tree without its history or local runtime data."""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_delivery_zip import ARCHIVE, validate_name, verify

def build():
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().strip('\0').split('\0')
    if not names or not names[0]:
        raise ValueError('Commit the reviewed source tree before building a release.')
    for name in names:
        validate_name(name)
        if not (ROOT/name).is_file() or (ROOT/name).is_symlink():
            raise ValueError('Expected an ordinary source file: ' + name)
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ARCHIVE.with_suffix('.building.zip')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(ROOT/name, name)
    count = verify(temporary)
    temporary.replace(ARCHIVE)
    print(f'Built {count} source files: {ARCHIVE}')

if __name__ == '__main__':
    build()

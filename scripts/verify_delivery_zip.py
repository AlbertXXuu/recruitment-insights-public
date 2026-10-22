"""Validate a current source release, excluding runtime data and collected texts."""
import argparse
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT/'outputs/recruitment-insights-source.zip'
SOFTWARE_FILES = ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'README.md', 'requirements.txt',
                  'app.py', 'core.py', 'accounts.py', 'seed_demo_accounts.py',
                  'data/demo_jobs.csv', 'data/demo_update.csv', 'data/import_template.csv',
                  'static/app.js', 'static/index.html', 'static/login.html', 'static/login.js', 'static/style.css',
                  'static/fonts/README.md', 'static/fonts/MiSans-font-license.pdf',
                  'static/fonts/MiSans-Regular.woff2', 'static/fonts/MiSans-Medium.woff2', 'static/fonts/MiSans-Semibold.woff2') + tuple(
                  sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'recruitment_insights').rglob('*.py')))

def validate_name(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name
            or any(x in {'.git', '__pycache__', '.venv', 'venv', 'outputs', 'deliverables'} for x in path.parts)
            or path.suffix.lower() in {'.sqlite', '.sqlite3', '.db', '.zip', '.log', '.pid', '.pyc'}
            or path.name.startswith(('.env', 'credentials.', 'token.'))
            or path.name in {'id_rsa', 'id_ed25519', 'public_jobs.json', 'cloudflare_snapshot.json'}):
        raise ValueError('Release contains an excluded path: ' + name)

def verify(path=ARCHIVE):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.testzip() is not None:
            raise ValueError('Duplicate names or corrupt ZIP')
        for name in names:
            validate_name(name)
        missing = set(SOFTWARE_FILES) - set(names)
        if missing:
            raise ValueError('Required source files missing: ' + ', '.join(sorted(missing)))
        return len(names)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', nargs='?', type=Path, default=ARCHIVE)
    print('Verified source files:', verify(parser.parse_args().archive))

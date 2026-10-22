"""Fetch a public job-board snapshot; no credentials or personal data."""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs?content=true'
if __name__ == '__main__':
    request = urllib.request.Request(URL, headers={'User-Agent': 'CourseRecruitmentAnalysis/1.0'})
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = response.read(20 * 1024 * 1024)
    data = json.loads(payload)
    target = ROOT / 'data' / 'cloudflare_snapshot.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    print(json.dumps({'jobs': len(data['jobs']), 'file': str(target), 'sample': [
        {'id': j['id'], 'title': j['title'], 'location': j['location'],
         'departments': j.get('departments'), 'fields': list(j)}
        for j in data['jobs'][:2]]}, ensure_ascii=False))

"""Regenerate the bundled county/place index from Census 2026 Gazetteer ZIPs."""
import csv
import io
import re
import urllib.request
import zipfile
from pathlib import Path

STATES = {'AL', 'GA', 'KY', 'NC', 'TN', 'WV'}
BASE = 'https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/'
OUT = Path(__file__).resolve().parents[1] / 'src/shade_node/assets/gazetteer_2026.csv'

def source_rows(kind):
    name = f'2026_Gaz_{"counties" if kind == "county" else "place"}_national.zip'
    with urllib.request.urlopen(BASE + name, timeout=30) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    with archive.open(archive.namelist()[0]) as data:
        yield from csv.DictReader(io.TextIOWrapper(data, encoding='utf-8-sig'), delimiter='|')

def clean(name, kind):
    if kind == 'county':
        return re.sub(r' (County|Parish|Borough|Census Area)$', '', name)
    return re.sub(r' (city|town|village|CDP|borough|municipality|balance)$', '', name)

def main():
    rows = []
    for kind in ('county', 'place'):
        for row in source_rows(kind):
            if kind == 'place' and row['USPS'] not in STATES:
                continue
            rows.append({'kind': kind, 'state': row['USPS'], 'geoid': row['GEOID'],
                         'name': clean(row['NAME'], kind), 'raw_name': row['NAME']})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['kind', 'state', 'geoid', 'name', 'raw_name'])
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: (row['state'], row['kind'], row['name'])))
    print(f'{len(rows)} Census names -> {OUT}')

if __name__ == '__main__':
    main()

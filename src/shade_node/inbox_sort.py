from datetime import datetime, timezone

def sorted_inbox(rows, key, descending=False):
    def value(row):
        if key == 'age':
            text = row.get('published') or ''
            try:
                return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
            except ValueError:
                return float('-inf')
        if key == 'remaining':
            text = row.get('expires') or ''
            try:
                return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
            except ValueError:
                return float('-inf')
        if key == 'status':
            return row.get('status', '')
        if key == 'families':
            return row.get('independent_families', 0)
        if key == 'confidence':
            return row.get('confidence_score', 0)
        if key in {'id', 'score'}:
            return row.get(key, 0)
        return str(row.get(key if key != 'title' else 'title', '')).casefold()
    return sorted(rows, key=lambda row: (value(row), row['id']), reverse=descending)

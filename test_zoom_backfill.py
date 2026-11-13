import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_module():
    spec = importlib.util.spec_from_file_location('backfill_under_test', Path(__file__).with_name('zoom_backfill.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'zoom_transcribe': types.SimpleNamespace(), 'requests': types.SimpleNamespace()}):
        spec.loader.exec_module(module)
    return module


class FakeSheets:
    def __init__(self):
        self.rows = [['ID', 'first_name', 'zoom_share_url', 'zoom_passcode', 'transcribation'],
                     ['2', 'second', 'url2', '', '']]
        self.writes = []

    def get(self, **kwargs):
        return types.SimpleNamespace(execute=lambda: {'values': [list(row) for row in self.rows]})

    def append(self, **kwargs):
        def execute():
            self.rows.insert(1, kwargs['body']['values'][0])
            return {}
        return types.SimpleNamespace(execute=execute)

    def batchUpdate(self, **kwargs):
        def execute():
            for update in kwargs['body']['data']:
                address = update['range'].split('!')[1]
                col, num = ord(address[0]) - 65, int(address[1:])
                self.writes.append(address)
                self.rows[num - 1][col] = update['values'][0][0]
            return {}
        return types.SimpleNamespace(execute=execute)


class BackfillTests(unittest.TestCase):
    def test_input_deduplicates_and_rejects_unbounded_selection(self):
        m = load_module()
        self.assertEqual(m.parse_ids('12, 13,12'), ['12', '13'])
        for value in ('', '12,abc', ','.join(['12'] * 201)):
            with self.assertRaises(ValueError):
                m.parse_ids(value)

    def test_ready_duplicate_wins(self):
        m, sheets = load_module(), FakeSheets()
        m.z = types.SimpleNamespace(MARKETING_SHEET_ID='sheet', DPD_TAB='dpd')
        sheets.rows.extend([['2', '', '', '', 'ready-doc'], ['2', '', '', '', '']])
        _, indexed = m.read_dpd(sheets)
        self.assertEqual(indexed['2'], (3, True))

    def test_writer_refreshes_rows_after_insert(self):
        m, sheets = load_module(), FakeSheets()
        source = [{'ID': '1'}, {'ID': '2'}]
        created = []
        def create_doc(docs, drive, title, text):
            created.append(title)
            return 'doc' + title
        m.z = types.SimpleNamespace(
            MARKETING_SHEET_ID='sheet', DPD_TAB='dpd', ZOOM_TAB='zoom', ZOOM_HEADER_ROW=1,
            google_clients=lambda: (sheets, None, None), read_tab=lambda *a: ([], source),
            parse_row=lambda row: {'share_url': 'url' + row['ID'], 'passcode': ''},
            deepgram_ok=lambda: True, create_doc=create_doc, col_letter=lambda n: chr(65+n), safe_cell=lambda v: v)
        secrets = {name: 'test' for name in ('APIFY_TOKEN', 'DEEPGRAM_TOKEN', 'GOOGLE_SERVICE_ACCOUNT_JSON',
                    'GOOGLE_OAUTH_CLIENT_ID', 'GOOGLE_OAUTH_CLIENT_SECRET', 'GOOGLE_OAUTH_REFRESH_TOKEN')}
        secrets['BACKFILL_DEALS'] = '1,2'
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', secrets), \
                patch.object(m, 'JOURNAL', Path(directory) / 'results.json'), \
                patch.object(m, 'prepare', side_effect=lambda row, _: {'deal': row['ID'], 'text': 'text', 'source': 'test'}), \
                patch.object(m.concurrent.futures, 'as_completed', side_effect=lambda jobs: iter(jobs)):
            self.assertEqual(m.main(), 0)
        self.assertEqual(created, ['1', '2'])
        self.assertIn('E3', sheets.writes)  # existing ID 2 moved from row 2 after insertion
        self.assertEqual(sheets.rows[2][0], '2')
        self.assertTrue(sheets.rows[2][4].endswith('/doc2/edit'))


if __name__ == '__main__':
    unittest.main()

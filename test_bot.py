import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
import bot


class Tests(unittest.TestCase):
    def test_parse(self):
        stamp = dt.datetime(2026, 10, 29, tzinfo=bot.MSK)
        self.assertEqual(bot.parse_report('29.10\n$44,4', stamp), ('2026-10-29', 4440))
        self.assertEqual(bot.parse_report('29.10\n88$', stamp), ('2026-10-29', 8800))
        self.assertIsNone(bot.parse_report('31.02\n88$', stamp))
        self.assertIsNone(bot.parse_report('29.10\n88$\n90$', stamp))
        self.assertEqual(bot.parse_report('31.12\n1$', dt.datetime(2027, 1, 1)), ('2026-12-31', 100))

    def test_import_and_winners(self):
        db = bot.database(':memory:')
        start = dt.date(2026, 10, 26)
        end = start + dt.timedelta(days=7)
        messages = []
        for mid, parent, amount in [(2, 1, '88'), (3, 1, '44,4'), (4, 999, '999'), (5, 2, '88')]:
            messages.append(dict(id=mid, reply_to_message_id=parent, from_id=f'user{mid}',
                                 date_unixtime='1793232000', text=f'29.10\n{amount}$', **{'from': f'Name {mid}'}))
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            path = Path(tmp) / 'result.json'
            path.write_text(json.dumps({'messages': messages}), encoding='utf-8')
            self.assertEqual(bot.import_history(db, path, 1, start, end, {'2': {'username': 'alice'}}), (3, 1))
            bot.import_history(db, path, 1, start, end, {})
        self.assertEqual(db.execute('SELECT COUNT(*) FROM reports').fetchone()[0], 3)
        report = bot.render(db, start, end)
        self.assertIn('Name 2', report)
        self.assertIn('Name 5', report)
        self.assertNotIn('Name 3', report)
        self.assertNotIn('999$', report)
        self.assertEqual(len(report.splitlines()), 7)

    def test_week(self):
        self.assertEqual(bot.previous_week(dt.datetime(2026, 11, 2)),
                         (dt.date(2026, 10, 26), dt.date(2026, 11, 2)))


if __name__ == '__main__':
    unittest.main()

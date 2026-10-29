"""Telegram shift reports. Python 3.11+, standard library only."""
import argparse
import datetime as dt
import html
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.request

MSK = dt.timezone(dt.timedelta(hours=3))


def parse_report(text, posted):
    date_match = re.search(r'(?m)^\s*(\d{1,2})[.](\d{1,2})(?:[.](\d{4}))?\s*$', text)
    amounts = re.findall(r'(?m)^\s*(?:\$\s*(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s*\$)\s*$', text)
    if not date_match or len(amounts) != 1:
        return None
    day, month, year = date_match.groups()
    try:
        if year:
            date = dt.date(int(year), int(month), int(day))
        else:
            candidates = []
            for y in range(posted.year - 1, posted.year + 2):
                try:
                    candidates.append(dt.date(y, int(month), int(day)))
                except ValueError:
                    pass
            date = min(candidates, key=lambda d: abs((d - posted.date()).days))
    except ValueError:
        return None
    value = next(x for x in amounts[0] if x).replace(',', '.')
    whole, _, fraction = value.partition('.')
    return date.isoformat(), int(whole) * 100 + int((fraction + '00')[:2])


def database(path):
    db = sqlite3.connect(path)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS reports (
      message_id INTEGER PRIMARY KEY, day TEXT, cents INTEGER,
      user_id INTEGER, name TEXT, username TEXT);
    CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
    ''')
    return db


def save(db, mid, text, posted, uid, name, username):
    parsed = parse_report(text, posted)
    if not parsed or not uid:
        return False
    db.execute('INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?,?)',
               (mid, *parsed, uid, name, username))
    return True


def previous_week(now):
    end = now.date() - dt.timedelta(days=now.weekday())
    return end - dt.timedelta(days=7), end


def render(db, start, end):
    lines = []
    for offset in range((end - start).days):
        day = start + dt.timedelta(days=offset)
        # Equal results: include all employees, deduplicating repeated reports.
        rows = db.execute('''SELECT DISTINCT user_id,name,username,cents FROM reports
          WHERE day=? AND cents=(SELECT MAX(cents) FROM reports WHERE day=?)
          ORDER BY user_id''', (day.isoformat(), day.isoformat())).fetchall()
        prefix = f'#повестка_дня_{day:%d_%m} - '
        if not rows:
            lines.append(prefix + 'Нет отчётов')
            continue
        winners = []
        for uid, name, username, cents in rows:
            person = html.escape(name)
            mention = '@' + html.escape(username.lstrip('@')) if username else f'<a href="tg://user?id={uid}">профиль</a>'
            amount = f'{cents / 100:.2f}'.rstrip('0').rstrip('.')
            winners.append(f'{person} {mention} - ${amount}')
        lines.append(prefix + 'Лучшая смена: ' + '; '.join(winners))
    return '\n'.join(lines)


def import_history(db, path, topic, start, end, people):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or not isinstance(data.get('messages'), list):
        raise ValueError('Нужен JSON-экспорт одного чата с полем messages')
    messages = {m['id']: m for m in data['messages']}

    def in_topic(message):
        seen = set()
        while message and message['id'] not in seen:
            seen.add(message['id'])
            if message['id'] == topic or message.get('message_thread_id') == topic:
                return True
            parent = message.get('reply_to_message_id')
            if parent == topic:
                return True
            message = messages.get(parent)
        return False

    count = unknown = 0
    for mid, m in messages.items():
        raw = m.get('text', '')
        text = raw if isinstance(raw, str) else ''.join(x if isinstance(x, str) else x.get('text', '') for x in raw)
        stamp = m.get('date_unixtime')
        if stamp is None:
            continue
        posted = dt.datetime.fromtimestamp(int(stamp), MSK)
        parsed = parse_report(text, posted)
        if not parsed or not (start.isoformat() <= parsed[0] < end.isoformat()):
            continue
        if not in_topic(m):
            unknown += 1
            continue
        uid = re.fullmatch(r'user(\d+)', m.get('from_id', ''))
        if not uid:
            continue
        uid = int(uid[1])
        p = people.get(str(uid), {})
        count += save(db, mid, text, posted, uid, p.get('name', m.get('from', str(uid))), p.get('username'))
    db.commit()
    return count, unknown


def api(token, method, **payload):
    request = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = json.load(response)
    if not data.get('ok'):
        raise RuntimeError('Telegram API request failed')
    return data['result']


def run(db, config):
    token = os.environ['BOT_TOKEN']
    chat, source, target = (int(config[k]) for k in ('chat_id', 'source_topic_id', 'target_topic_id'))
    if source == target:
        raise ValueError('Укажите разные исходный и итоговый топики')
    while True:
        try:
            now = dt.datetime.now(MSK)
            start, end = previous_week(now)
            sent = db.execute('SELECT value FROM state WHERE key=?', ('sent',)).fetchone()
            due = dt.datetime.combine(end, dt.time(10), MSK)
            if now >= due and (not sent or sent[0] != start.isoformat()):
                count = db.execute('SELECT COUNT(*) FROM reports WHERE day>=? AND day<?',
                                   (start.isoformat(), end.isoformat())).fetchone()[0]
                if count:
                    api(token, 'sendMessage', chat_id=chat, message_thread_id=target,
                        text=render(db, start, end), parse_mode='HTML')
                    db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('sent', start.isoformat()))
                    db.commit()
            offset = db.execute('SELECT value FROM state WHERE key=?', ('offset',)).fetchone()
            updates = api(token, 'getUpdates', offset=int(offset[0]) if offset else 0,
                          timeout=20, allowed_updates=['message', 'edited_message'])
            for update in updates:
                m = update.get('message') or update.get('edited_message')
                if m and m['chat']['id'] == chat and m.get('message_thread_id') == source:
                    u = m.get('from', {})
                    if not u.get('is_bot'):
                        # Editing an old report can invalidate its original content.
                        db.execute('DELETE FROM reports WHERE message_id=?', (m['message_id'],))
                        save(db, m['message_id'], m.get('caption', m.get('text', '')),
                             dt.datetime.fromtimestamp(m['date'], MSK), u.get('id'),
                             ' '.join(filter(None, [u.get('first_name'), u.get('last_name')])), u.get('username'))
                db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('offset', str(update['update_id'] + 1)))
                db.commit()
        except Exception as exc:
            # Do not log the request URL, which contains the bot token.
            logging.error('Сбой (%s); повтор через 10 секунд', type(exc).__name__)
            time.sleep(10)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['run', 'import', 'preview'])
    p.add_argument('--config', default='config.json')
    p.add_argument('--db', default='reports.sqlite3')
    p.add_argument('--file')
    p.add_argument('--week', help='Понедельник нужной недели, YYYY-MM-DD; по умолчанию прошлая')
    args = p.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    db = database(args.db)
    start, end = previous_week(dt.datetime.now(MSK))
    if args.week:
        start = dt.date.fromisoformat(args.week)
        if start.weekday() != 0:
            p.error('--week должен быть понедельником')
        end = start + dt.timedelta(days=7)
    if args.command == 'import':
        if not args.file:
            p.error('Укажите --file result.json')
        print('Импортировано; пропущено из-за другого/неизвестного топика:',
              import_history(db, args.file, int(config['source_topic_id']), start, end, config.get('people', {})))
    elif args.command == 'preview':
        print(render(db, start, end))
    else:
        run(db, config)


if __name__ == '__main__':
    main()

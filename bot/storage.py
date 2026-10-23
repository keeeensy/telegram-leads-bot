"""SQLite: состояние диалогов, заявки и связь "сообщение менеджеру → клиент"."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .engine import Session


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, user_id INTEGER,
                username TEXT, fields TEXT NOT NULL, status TEXT DEFAULT 'новая', taken_by TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS relay (
                chat_id INTEGER, message_id INTEGER, user_id INTEGER, PRIMARY KEY (chat_id, message_id));
        """)

    # ----- диалоги -----

    def session(self, user_id: int) -> Session:
        row = self.db.execute("SELECT data FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        return Session.from_dict(json.loads(row["data"])) if row else Session()

    def save(self, user_id: int, s: Session):
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)",
                        (user_id, json.dumps(s.to_dict(), ensure_ascii=False), s.updated))
        self.db.commit()

    def to_remind(self, older_than: float) -> list[int]:
        rows = self.db.execute("SELECT user_id, data FROM sessions WHERE updated < ? AND updated > ?",
                               (older_than, older_than - 3 * 86400)).fetchall()
        result = []
        for row in rows:
            data = json.loads(row["data"])
            if data.get("active") and not data.get("reminded"):
                result.append(row["user_id"])
        return result

    # ----- заявки -----

    def add_lead(self, user_id: int, username: str, fields: list) -> int:
        cur = self.db.execute("INSERT INTO leads (created, user_id, username, fields) VALUES (?,?,?,?)",
                              (time.time(), user_id, username, json.dumps(fields, ensure_ascii=False)))
        self.db.commit()
        return cur.lastrowid

    def take_lead(self, lead_id: int, who: str) -> bool:
        cur = self.db.execute("UPDATE leads SET status='в работе', taken_by=? WHERE id=? AND status='новая'",
                              (who, lead_id))
        self.db.commit()
        return cur.rowcount > 0

    def leads(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM leads ORDER BY id").fetchall()
        return [dict(r, fields=json.loads(r["fields"])) for r in rows]

    # ----- переписка менеджера с клиентом -----

    def link(self, chat_id: int, message_id: int, user_id: int):
        self.db.execute("INSERT OR REPLACE INTO relay VALUES (?,?,?)", (chat_id, message_id, user_id))
        self.db.commit()

    def linked_user(self, chat_id: int, message_id: int) -> int | None:
        row = self.db.execute("SELECT user_id FROM relay WHERE chat_id=? AND message_id=?",
                              (chat_id, message_id)).fetchone()
        return row["user_id"] if row else None


def leads_xlsx(leads: list[dict], path: str) -> str:
    """Выгрузка заявок в Excel: каждая графа заявки - отдельная колонка."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    columns: list[str] = []
    for lead in leads:
        for label, _ in lead["fields"]:
            if label not in columns:
                columns.append(label)
    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    ws.append(["№", "Дата", "Telegram", "Статус", "Кто взял"] + columns)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for lead in leads:
        values = {}
        for label, value in lead["fields"]:
            values[label] = f"{values[label]}; {value}" if label in values else value
        ws.append([lead["id"], time.strftime("%d.%m.%Y %H:%M", time.localtime(lead["created"])),
                   ("@" + lead["username"]) if lead["username"] else str(lead["user_id"]),
                   lead["status"], lead["taken_by"]] + [values.get(c, "") for c in columns])
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 22
        for cell in col:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "B2"
    wb.save(path)
    return path

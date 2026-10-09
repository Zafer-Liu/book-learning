"""Persisted self-test and due-review API regressions."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from study.app import create_app, now, uid
from study.database import BUILTIN_OWNER, Database


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for patcher in (
            patch.dict(os.environ, {"STUDY_SEED_BUILTIN": "0"}),
            patch("study.app.EmbeddingClient", return_value=Mock(configured=False)),
            patch("study.app.Tutor", return_value=Mock(configured=False)),
            patch("study.app.ThreadPoolExecutor", return_value=Mock()),
            patch("study.app.threading.Thread", return_value=Mock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = create_app({"TESTING": True, "DATA_ROOT": self.root,
                               "SECRET_KEY": "review-test-secret-" * 4,
                               "SESSION_COOKIE_SECURE": False, "TEST_CODES": ()})
        self.db = self.app.extensions["database"]
        self.a, self.a_id, self.a_csrf = self.user("review_a")
        self.b, _, self.b_csrf = self.user("review_b")
        self.book_id, self.conversation_id, self.message_id = uid(), uid(), uid()
        answer = {"paragraphs": [], "quiz": [{"question": "什么是检索练习？",
                 "answer": "先尝试回忆。", "explanation": "依据原文。", "citations": ["C1"]}],
                  "citations": [{"label": "C1", "section": "第一章"}], "grounded": True}
        with self.db.connect() as db:
            db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at) "
                       "VALUES(?,?,?,?,?,'ready',?)",
                       (self.book_id, self.a_id, "Review book", "source.md", "source.md", now()))
            db.execute("INSERT INTO conversations(id,owner_id,book_id,title,created_at) VALUES(?,?,?,?,?)",
                       (self.conversation_id, self.a_id, self.book_id, "Self test", now()))
            db.execute("INSERT INTO messages(id,owner_id,book_id,conversation_id,role,content,mode,payload,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?)",
                       (self.message_id, self.a_id, self.book_id, self.conversation_id, "assistant", "", "quiz",
                        json.dumps(answer, ensure_ascii=False), now()))
        self.path = (f"/api/books/{self.book_id}/conversations/{self.conversation_id}"
                     f"/messages/{self.message_id}/quiz/0")

    def user(self, name):
        user_id, csrf = uid(), uid()
        with self.db.connect() as db:
            db.execute("INSERT INTO users(id,username,username_key,password_hash,created_at) VALUES(?,?,?,?,?)",
                       (user_id, name, name, "unused", now()))
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"], session["csrf"] = user_id, csrf
        return client, user_id, csrf

    def save(self, payload, client=None, csrf=None, path=None):
        return (client or self.a).patch(path or self.path, json=payload,
                                        headers={"X-CSRF-Token": csrf or self.a_csrf})

    def test_attempt_survives_history_and_enters_review_queue(self):
        initial = self.a.get(f"/api/books/{self.book_id}/review").get_json()
        self.assertEqual(initial, {"items": [], "due_count": 0, "due_total": 0})
        result = self.save({"draft": "先想答案", "rating": "review"})
        self.assertEqual(result.status_code, 200)
        due = result.get_json()["attempt"]["due_at"]
        self.assertEqual(result.get_json()["review_due"], 1)
        history = self.a.get(f"/api/books/{self.book_id}/conversations/{self.conversation_id}").get_json()
        self.assertEqual(history["messages"][0]["quiz_attempts"][0]["draft"], "先想答案")
        queue = self.a.get(f"/api/books/{self.book_id}/review").get_json()
        self.assertEqual(queue["due_count"], 1)
        self.assertEqual(queue["items"][0]["question"], "什么是检索练习？")
        self.assertNotIn("draft", queue["items"][0])
        updated = self.save({"draft": "补充我的答案", "rating": "review"}).get_json()
        self.assertEqual(updated["attempt"]["due_at"], due)
        self.assertEqual(self.a.get(f"/api/books/{self.book_id}").get_json()["stats"]["review_due"], 1)
        reopened = Database(self.root)
        with reopened.connect() as db:
            self.assertEqual(db.execute("SELECT draft FROM quiz_attempts WHERE message_id=?",
                                        (self.message_id,)).fetchone()[0], "补充我的答案")

    def test_review_schedules_next_day_and_understood_schedules_later(self):
        self.save({"draft": "第一次", "rating": "review"})
        next_day = self.save({"draft": "再次回忆", "rating": "review", "reviewed": True}).get_json()
        self.assertEqual(next_day["review_due"], 0)
        self.assertEqual(self.a.get(f"/api/books/{self.book_id}/review").get_json()["items"], [])
        understood = self.save({"draft": "现在记住了", "rating": "understood", "reviewed": True}).get_json()
        self.assertGreater(understood["attempt"]["due_at"], next_day["attempt"]["due_at"])
        self.assertEqual(understood["review_due"], 0)
        with self.db.connect() as db:
            db.execute("UPDATE quiz_attempts SET due_at=? WHERE message_id=?", (now(), self.message_id))
        self.assertEqual(self.a.get(f"/api/books/{self.book_id}/review").get_json()["due_count"], 1)

    def test_permissions_validation_and_cascade(self):
        self.assertEqual(self.save({"draft": "x", "rating": "review"}, client=self.b,
                                   csrf=self.b_csrf).status_code, 404)
        self.assertEqual(self.b.get(f"/api/books/{self.book_id}/review").status_code, 404)
        for payload in ({"draft": "x", "rating": "bad"}, {"draft": 1, "rating": "review"},
                        {"draft": "x", "rating": "review", "reviewed": 1},
                        {"draft": "x", "rating": "review", "unknown": True}):
            self.assertEqual(self.save(payload).status_code, 400)
        self.assertEqual(self.save({"draft": "x", "rating": "review"},
                                   path=self.path[:-1] + "1").status_code, 404)
        self.assertEqual(self.save({"draft": "x", "rating": "review"}).status_code, 200)
        with self.db.connect() as db:
            db.execute("DELETE FROM conversations WHERE id=?", (self.conversation_id,))
            self.assertEqual(db.execute("SELECT count(*) FROM quiz_attempts").fetchone()[0], 0)

    def test_global_queue_collects_books_without_cross_account_leak(self):
        self.save({"draft": "first", "rating": "review"})
        second_book, second_conversation, second_message = uid(), uid(), uid()
        with self.db.connect() as db:
            db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at) "
                       "VALUES(?,?,?,?,?,'ready',?)",
                       (second_book, self.a_id, "Second book", "second.md", "second.md", now()))
            db.execute("INSERT INTO conversations(id,owner_id,book_id,title,created_at) VALUES(?,?,?,?,?)",
                       (second_conversation, self.a_id, second_book, "Second quiz", now()))
            db.execute("INSERT INTO messages(id,owner_id,book_id,conversation_id,role,content,mode,payload,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?)",
                       (second_message, self.a_id, second_book, second_conversation, "assistant", "", "quiz",
                        json.dumps({"quiz": [{"question": "第二本书的问题", "answer": "答案", "explanation": "解析"}]},
                                   ensure_ascii=False), now()))
        path = (f"/api/books/{second_book}/conversations/{second_conversation}"
                f"/messages/{second_message}/quiz/0")
        result = self.save({"draft": "second", "rating": "review"}, path=path).get_json()
        self.assertEqual(result["review_due_total"], 2)
        queue = self.a.get("/api/review").get_json()
        self.assertEqual(queue["due_count"], 2)
        self.assertEqual({item["book_title"] for item in queue["items"]}, {"Review book", "Second book"})
        self.assertEqual(self.b.get("/api/review").get_json(), {"due_count": 0, "items": []})
        books = self.a.get("/api/books").get_json()["books"]
        self.assertEqual({book["title"]: book["review_due"] for book in books},
                         {"Review book": 1, "Second book": 1})

    def test_shared_book_keeps_each_accounts_review_private(self):
        with self.db.connect() as db:
            db.execute("INSERT INTO users(id,username,username_key,password_hash,created_at) VALUES(?,?,?,?,?)",
                       (BUILTIN_OWNER, "builtin", "builtin", "unused", now()))
            db.execute("UPDATE books SET owner_id=? WHERE id=?", (BUILTIN_OWNER, self.book_id))
        self.assertEqual(self.save({"draft": "my note", "rating": "review"}).status_code, 200)
        self.assertEqual(self.a.get(f"/api/books/{self.book_id}/review").get_json()["due_count"], 1)
        self.assertEqual(self.b.get(f"/api/books/{self.book_id}/review").get_json()["due_count"], 0)
        self.assertEqual(self.save({"draft": "other", "rating": "review"}, client=self.b,
                                   csrf=self.b_csrf).status_code, 404)


if __name__ == "__main__":
    unittest.main()

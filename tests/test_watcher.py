import json
from unittest import mock

import pytest

import tiktok_repost_watcher as w


def item(vid, author="someone", desc="hello"):
    return {"id": vid, "author": {"uniqueId": author}, "desc": desc, "video": {"cover": "https://c/x.jpg"}}


class FakeResp:
    def __init__(self, text="", data=None):
        self.text = text
        self._data = data
        self.content = (json.dumps(data) if data is not None else text).encode()

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, items, sec_uid="SEC123"):
        self.items = items
        self.sec_uid = sec_uid
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url == w.REPOST_API:
            assert params["secUid"] == self.sec_uid
            return FakeResp(data={"statusCode": 0, "itemList": self.items})
        payload = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": {"user": {"secUid": self.sec_uid}}}}}
        return FakeResp(text=f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
                             f'{json.dumps(payload)}</script>')


@pytest.fixture
def notified(monkeypatch):
    sent = []
    monkeypatch.setattr(w, "notify", lambda user, repost: sent.append((user, repost.id)))
    return sent


def test_resolve_sec_uid():
    assert w.resolve_sec_uid(FakeSession([]), "x") == "SEC123"


def test_first_run_is_silent_then_new_reposts_notify_oldest_first(notified):
    state = {}
    session = FakeSession([item("2"), item("1")])
    assert w.check_account(session, "alice", state, notify_existing=False) == 0
    assert notified == []
    assert state["alice"]["seen"] == ["2", "1"]

    session.items = [item("4"), item("3"), item("2"), item("1")]
    assert w.check_account(session, "alice", state, notify_existing=False) == 2
    assert notified == [("alice", "3"), ("alice", "4")]
    assert state["alice"]["seen"][:4] == ["4", "3", "2", "1"]
    # secUid is cached: the profile page is only fetched once.
    assert sum(1 for c in session.calls if c != w.REPOST_API) == 1


def test_notify_existing_on_first_run(notified):
    w.check_account(FakeSession([item("2"), item("1")]), "alice", {}, notify_existing=True)
    assert notified == [("alice", "1"), ("alice", "2")]


def test_removed_repost_does_not_renotify(notified):
    state = {"alice": {"sec_uid": "SEC123", "seen": ["3", "2", "1"]}}
    w.check_account(FakeSession([item("3"), item("1")]), "alice", state, notify_existing=False)
    assert notified == []
    assert "2" in state["alice"]["seen"]


def test_remember_is_bounded():
    seen = [str(i) for i in range(w.MAX_REMEMBERED)]
    out = w.remember(seen, [w.Repost("new", "a", "", "")])
    assert out[0] == "new" and len(out) == w.MAX_REMEMBERED


def test_api_error_raises():
    session = FakeSession([])
    session.get = lambda *a, **k: FakeResp(data={"statusCode": 10222, "status_msg": "private"})
    with pytest.raises(w.WatcherError):
        w.fetch_reposts(session, "SEC")


def test_failed_check_keeps_state_and_returns_error(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"alice": {"sec_uid": "S", "seen": ["1"]}}))

    def boom(*a, **k):
        raise w.WatcherError("rate limited")

    monkeypatch.setattr(w, "fetch_reposts", boom)
    assert w.run_once(["alice"], path, False) is False
    assert json.loads(path.read_text())["alice"]["seen"] == ["1"]


def test_notifiers_send_expected_requests(monkeypatch, capsys):
    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/hook")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("WEBHOOK_URL", "https://example/hook")
    with mock.patch.object(w.requests, "post") as post:
        w.notify("alice", w.Repost("99", "bob", "a cool video", "https://c/x.jpg"))
    urls = [c.args[0] for c in post.call_args_list]
    assert urls == ["https://ntfy.sh/my-topic", "https://discord/hook",
                    "https://api.telegram.org/bottok/sendMessage", "https://example/hook"]
    assert post.call_args_list[0].kwargs["headers"]["Click"] == "https://www.tiktok.com/@bob/video/99"
    assert "@alice reposted a video" in capsys.readouterr().out


def test_one_failing_notifier_does_not_block_others(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "t")
    monkeypatch.setenv("WEBHOOK_URL", "https://example/hook")
    calls = []

    def post(url, **kw):
        calls.append(url)
        if "ntfy" in url:
            raise w.requests.ConnectionError("down")
        return FakeResp(data={})

    monkeypatch.setattr(w.requests, "post", post)
    w.notify("alice", w.Repost("1", "bob", "", ""))
    assert calls == ["https://ntfy.sh/t", "https://example/hook"]

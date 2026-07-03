from autotrader.alerts import AlertSink


def test_send_posts_and_returns_true_on_2xx():
    posted = []
    sink = AlertSink("http://x", post=lambda url, payload: posted.append(payload) or 200)
    assert sink.send("hello") is True
    assert posted == [{"text": "hello"}]


def test_no_url_is_noop_false():
    assert AlertSink(None).send("hello") is False


def test_post_exception_never_raises():
    def boom(url, payload):
        raise OSError("network down")
    assert AlertSink("http://x", post=boom).send("hello") is False


def test_key_dedupes_until_reset():
    posted = []
    sink = AlertSink("http://x", post=lambda u, p: posted.append(p) or 200)
    assert sink.send("watchdog down", key="watchdog") is True
    assert sink.send("watchdog down", key="watchdog") is False   # suppressed
    assert len(posted) == 1
    sink.reset("watchdog")
    assert sink.send("watchdog down again", key="watchdog") is True
    assert len(posted) == 2

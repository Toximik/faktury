"""Favicon, touch icons and robots.txt are served publicly at the site root."""

import pytest


@pytest.mark.parametrize("path, content_type, magic", [
    ("/favicon.ico", "image/x-icon", b"\x00\x00\x01\x00"),
    ("/apple-touch-icon.png", "image/png", b"\x89PNG"),
    ("/apple-touch-icon-precomposed.png", "image/png", b"\x89PNG"),
])
def test_icons_served_without_login(client, path, content_type, magic):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"] == content_type
    assert r.content.startswith(magic)


def test_robots_txt_disallows_everything(client):
    r = client.get("/robots.txt", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "Disallow: /" in r.text


def test_login_page_links_icons(client):
    html = client.get("/login").text
    assert 'rel="icon"' in html
    assert 'rel="apple-touch-icon"' in html

import pytest
from flask import Flask

from core.fork import artist_names, store


@pytest.fixture
def client(monkeypatch):
    import core.profile_context as profile_context

    # admin_only is applied at import time; make it a pass-through for the test app.
    monkeypatch.setattr(profile_context, "admin_only", lambda fn: fn)
    import importlib

    import api.fork as fork_api

    fork_api = importlib.reload(fork_api)
    app = Flask(__name__)
    app.register_blueprint(fork_api.create_blueprint())
    yield app.test_client()
    monkeypatch.undo()
    importlib.reload(fork_api)


def test_settings_round_trip_keeps_only_known_keys(client, fork_env):
    data = client.get("/api/fork/settings").get_json()
    assert data["settings"]["models"] == {"search_terms": "qwen3.5:9b", "names": "qwen3.5:9b", "lyrics": "qwen3.5:9b"}
    resp = client.post("/api/fork/settings", json={
        "models": {"lyrics": "gemma4:12b", "bogus": "x"},
        "translate": {"template": "{translated} [{original}]", "titles": False},
        "search_terms": {"max_variants": "2"},
        "unknown_section": {"a": 1},
    }).get_json()
    assert resp["settings"]["models"]["lyrics"] == "gemma4:12b"
    assert resp["settings"]["models"]["names"] == "qwen3.5:9b"
    assert resp["settings"]["translate"]["titles"] is False
    assert resp["settings"]["search_terms"]["max_variants"] == 2
    assert fork_env.data["fork"] == {
        "models": {"lyrics": "gemma4:12b"},
        "translate": {"template": "{translated} [{original}]", "titles": False},
        "search_terms": {"max_variants": 2},
    }


def test_editing_a_translation_marks_it_user_edited(client):
    store.save_translation("album", "夜曲", "Night Song", model="m")
    resp = client.put("/api/fork/translations", json={"kind": "album", "original": "夜曲 (Deluxe)", "translated": "Nocturne"})
    assert resp.get_json() == {"success": True, "original": "夜曲", "display": "Nocturne (夜曲)"}
    listed = client.get("/api/fork/translations?kind=album").get_json()
    assert listed["total"] == 1
    assert listed["items"][0]["translated"] == "Nocturne" and listed["items"][0]["user_edited"] == 1
    assert client.delete("/api/fork/translations", json={"kind": "album", "original": "夜曲"}).status_code == 200
    assert client.delete("/api/fork/translations", json={"kind": "album", "original": "夜曲"}).status_code == 404


def test_translation_validation(client):
    assert client.put("/api/fork/translations", json={"kind": "artist", "original": "a", "translated": "b"}).status_code == 400
    assert client.put("/api/fork/translations", json={"kind": "album", "original": "a", "translated": " "}).status_code == 400


def test_artist_rules_crud_and_lookup(client, monkeypatch):
    monkeypatch.setattr(artist_names, "lookup_musicbrainz", lambda name: {"replacement": "Jay Chou", "mbid": "m"})
    assert client.post("/api/fork/artist-names/lookup", json={"original": "周杰倫"}).get_json()["replacement"] == "Jay Chou"
    assert store.get_artist_name("周杰倫") is None  # lookup alone saves nothing
    assert client.put("/api/fork/artist-names", json={"original": "周杰倫", "replacement": "Jay Chou"}).status_code == 200
    store.save_artist_name("無名", "", "none")
    items = client.get("/api/fork/artist-names").get_json()["items"]
    assert [(i["original"], i["replacement"], i["source"]) for i in items] == [("周杰倫", "Jay Chou", "manual")]
    assert client.delete("/api/fork/artist-names", json={"original": "周杰倫"}).status_code == 200

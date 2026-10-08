"""The artist-level folder is named after the first album artist only."""

import os

from core.fork import artist_names, hooks
from core.imports import paths


def _join(*parts):
    return os.path.join(*parts)


def test_several_album_artists_are_filed_under_the_first(fork_env, monkeypatch):
    monkeypatch.setattr(artist_names, "is_single_artist", lambda name: name == "Simon & Garfunkel")
    context = {"albumartist": "Monster Siren Records, Obadiah Brown-Beach & Cristina Vee", "album": "Spark for Dream"}
    folder = _join("Monster Siren Records, Obadiah Brown-Beach & Cristina Vee",
                   "Monster Siren Records, Obadiah Brown-Beach & Cristina Vee - Spark for Dream")
    assert hooks.first_artist_folder((folder, "01 - Spark for Dream"), context) == (
        _join("Monster Siren Records", "Monster Siren Records, Obadiah Brown-Beach & Cristina Vee - Spark for Dream"),
        "01 - Spark for Dream")                                 # the album folder keeps the full credit

    one = (_join("Polyphia", "Polyphia - Album"), "01 - Song")
    assert hooks.first_artist_folder(one, {"albumartist": "Polyphia"}) == one
    duo = (_join("Simon & Garfunkel", "Simon & Garfunkel - Album"), "01 - Song")
    assert hooks.first_artist_folder(duo, {"albumartist": "Simon & Garfunkel"}) == duo      # one artist
    # a first folder that is not the album artist alone is left as it is
    other = (_join("Music", "A, B", "Album"), "01 - Song")
    assert hooks.first_artist_folder(other, {"albumartist": "A, B"}) == other
    assert hooks.first_artist_folder((_join("A, B", "Album"), "x"), {"albumartist": "A, B"}, "playlist_path") \
        == (_join("A, B", "Album"), "x")
    assert hooks.first_artist_folder((None, None), {"albumartist": "A, B"}) == (None, None)
    fork_env.set("fork.paths.first_album_artist_folder", False)
    assert hooks.first_artist_folder((folder, "x"), context) == (folder, "x")


def test_the_path_built_from_the_template_uses_it(fork_env, monkeypatch):
    monkeypatch.setattr(artist_names, "is_single_artist", lambda name: False)

    class Settings:
        def get(self, key, default=None):
            return {"file_organization.enabled": True,
                    "file_organization.templates": {"album_path": "$albumartist/$albumartist - $album/$track - $title"},
                    "file_organization.collab_artist_mode": "all"}.get(key, default)

    monkeypatch.setattr(paths, "_get_config_manager", lambda: Settings())
    folder, name = paths.get_file_path_from_template(
        {"albumartist": "A, B", "artist": "A", "album": "Album", "title": "Song", "track_number": 1}, "album_path")
    assert (folder, name) == (_join("A", "A, B - Album"), "01 - Song")

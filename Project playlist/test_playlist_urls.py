import unittest

from server import normalize_playlist_url


class PlaylistUrlNormalizationTests(unittest.TestCase):
    def test_watch_url_with_list_param_is_normalized(self):
        self.assertEqual(
            normalize_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc123"),
            "https://www.youtube.com/playlist?list=PLabc123",
        )

    def test_music_playlist_url_is_preserved(self):
        self.assertEqual(
            normalize_playlist_url("https://music.youtube.com/playlist?list=PLxyz789"),
            "https://music.youtube.com/playlist?list=PLxyz789",
        )


if __name__ == "__main__":
    unittest.main()

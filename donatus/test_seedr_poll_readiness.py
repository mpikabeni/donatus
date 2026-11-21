from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from services import seedr


class SeedrPollReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_materialized_full_folder_bypasses_stale_99_percent_meter(self):
        folder = {"id": 42, "name": "Show Episode 01", "size": 1_000}
        root = {
            "folders": [folder],
            "torrents": [{
                "id": 7,
                "name": "Show Episode 01",
                "progress": 99.0,
                "size": 1_000,
            }],
        }
        contents = {
            "files": [{"id": 100, "name": "episode.mkv", "size": 1_000}],
            "folders": [],
        }

        with (
            patch.object(seedr, "_root", AsyncMock(return_value=root)),
            patch.object(seedr, "_list_folder", AsyncMock(return_value=contents)),
        ):
            result = await seedr._poll(
                "user", "password", 7, set(), timeout_s=1, interval=0,
            )

        self.assertEqual(result, folder)

    async def test_unknown_size_folder_must_be_stable_for_three_polls(self):
        folder = {"id": 43, "name": "Show Episode 02", "size": 0}
        root = {
            "folders": [folder],
            "torrents": [{
                "id": 8,
                "name": "Show Episode 02",
                "progress": 98.0,
                "size": 0,
            }],
        }
        contents = {
            "files": [{"id": 101, "name": "episode.mkv", "size": 900}],
            "folders": [],
        }
        root_mock = AsyncMock(return_value=root)

        with (
            patch.object(seedr, "_root", root_mock),
            patch.object(seedr, "_list_folder", AsyncMock(return_value=contents)),
        ):
            result = await seedr._poll(
                "user", "password", 8, set(), timeout_s=1, interval=0,
            )

        self.assertEqual(result, folder)
        self.assertEqual(root_mock.await_count, 3)

    async def test_concrete_id_does_not_latch_unrelated_active_torrent(self):
        unrelated_folder = {"id": 90, "name": "Other Show", "size": 500}
        wanted_folder = {"id": 91, "name": "Wanted Show", "size": 700}
        roots = [
            {
                "folders": [unrelated_folder],
                "torrents": [{
                    "id": 99, "name": "Other Show", "progress": 100.0, "size": 500,
                }],
            },
            {
                "folders": [unrelated_folder, wanted_folder],
                "torrents": [{
                    "id": 10, "name": "Wanted Show", "progress": 100.0, "size": 700,
                }],
            },
        ]

        with patch.object(seedr, "_root", AsyncMock(side_effect=roots)):
            result = await seedr._poll(
                "user", "password", 10, set(), timeout_s=1, interval=0,
            )

        self.assertEqual(result, wanted_folder)

    async def test_active_torrent_does_not_claim_an_unrelated_sole_folder(self):
        unrelated_folder = {"id": 92, "name": "Other Release", "size": 500}
        wanted_folder = {"id": 93, "name": "Wanted Release", "size": 700}
        roots = [
            {
                "folders": [unrelated_folder],
                "torrents": [{
                    "id": 11, "name": "Wanted Release", "progress": 99.0, "size": 700,
                }],
            },
            {
                "folders": [unrelated_folder, wanted_folder],
                "torrents": [{
                    "id": 11, "name": "Wanted Release", "progress": 100.0, "size": 700,
                }],
            },
        ]

        with patch.object(seedr, "_root", AsyncMock(side_effect=roots)):
            result = await seedr._poll(
                "user", "password", 11, set(), timeout_s=1, interval=0,
            )

        self.assertEqual(result, wanted_folder)


class SeedrLargestVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_pack_resolves_only_the_largest_video_url(self):
        folders = {
            1: {
                "files": [
                    {"id": 1, "name": "[Pack] Episode 01.mkv", "size": 1_000, "url": ""},
                    {"id": 2, "name": "sample.mkv", "size": 20, "url": ""},
                    {"id": 3, "name": "notes.txt", "size": 2, "url": ""},
                ],
                "folders": [{"id": 2, "name": "extras", "size": 0}],
            },
            2: {
                "files": [
                    {"id": 4, "name": "[Pack] Episode 02.mkv", "size": 2_000, "url": ""},
                ],
                "folders": [],
            },
        }

        async def list_folder(_user, _password, folder_id):
            return folders[folder_id]

        rename = AsyncMock(return_value=True)
        fetch_url = AsyncMock(return_value="https://seedr.example/episode02.mkv")
        with (
            patch.object(seedr, "_list_folder", side_effect=list_folder),
            patch.object(seedr, "_rename_file", rename),
            patch.object(seedr, "_file_url_fallback", fetch_url),
        ):
            files = await seedr._collect_largest_video("user", "password", 1)

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["orig_name"], "[Pack] Episode 02.mkv")
        rename.assert_awaited_once()
        self.assertEqual(rename.await_args.args[2], 4)
        fetch_url.assert_awaited_once_with("user", "password", 4)


if __name__ == "__main__":
    unittest.main()

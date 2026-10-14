"""discord_out.py: Discord本文2000字制限の分割送信"""
import unittest
from unittest import mock

import _support


class SplitChunksTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.out = self._iso.import_module("companion.discord_out")

    def test_short_text_single_chunk(self):
        self.assertEqual(self.out._split_chunks("こんにちは"), ["こんにちは"])

    def test_long_text_splits_at_newline(self):
        # 改行位置で割る（境界の改行自体はメッセージ区切りとして消費される）
        line = "あ" * 1500
        text = line + "\n" + line + "\n" + line
        chunks = self.out._split_chunks(text)
        self.assertEqual(chunks, [line, line, line])
        for c in chunks:
            self.assertLessEqual(len(c), self.out._CHUNK_LIMIT)

    def test_long_text_no_newline_hard_cuts(self):
        text = "x" * 5000
        chunks = self.out._split_chunks(text)
        self.assertTrue(len(chunks) >= 3)
        for c in chunks:
            self.assertLessEqual(len(c), self.out._CHUNK_LIMIT)


class SendChannelMessageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.out = self._iso.import_module("companion.discord_out")

    async def test_long_message_sends_multiple_posts(self):
        posts = []

        class Res:
            status_code = 200
            text = ""

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                posts.append(json["content"])
                return Res()

        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "t"}), \
                mock.patch.object(self.out.httpx, "AsyncClient",
                                  return_value=Client()):
            await self.out.send_channel_message(1, "あ" * 4000)
        self.assertGreaterEqual(len(posts), 2)
        self.assertEqual("".join(posts), "あ" * 4000)


if __name__ == "__main__":
    unittest.main()

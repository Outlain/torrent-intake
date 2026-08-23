from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.qbt import QbtService


class QbtSessionReuseTests(TestCase):
    def setUp(self) -> None:
        QbtService._drop_shared_client()

    def tearDown(self) -> None:
        QbtService._drop_shared_client()

    @patch("app.qbt.QbtService._log_in", autospec=True)
    @patch("app.qbt.qbittorrentapi.Client")
    def test_client_is_shared_across_service_instances(self, client_cls, log_in) -> None:
        client = MagicMock()
        client_cls.return_value = client

        first = QbtService()
        second = QbtService()

        self.assertIs(first.client(), client)
        self.assertIs(second.client(), client)
        self.assertEqual(client_cls.call_count, 1)
        self.assertEqual(log_in.call_count, 1)

    @patch("app.qbt.QbtService._log_in", autospec=True)
    @patch("app.qbt.qbittorrentapi.Client")
    def test_concurrent_client_requests_create_one_session(self, client_cls, log_in) -> None:
        client = MagicMock()
        client_cls.return_value = client
        services = [QbtService() for _ in range(20)]

        with ThreadPoolExecutor(max_workers=20) as pool:
            clients = list(pool.map(lambda service: service.client(), services))

        self.assertTrue(all(item is client for item in clients))
        self.assertEqual(client_cls.call_count, 1)
        self.assertEqual(log_in.call_count, 1)

    @patch("app.qbt.QbtService._log_in", autospec=True)
    @patch("app.qbt.qbittorrentapi.Client")
    def test_multiple_operations_reuse_same_authenticated_client(self, client_cls, log_in) -> None:
        torrent = SimpleNamespace(hash="abc", tags="torrent_intake")
        client = MagicMock()
        client.torrents_info.return_value = [torrent]
        client_cls.return_value = client

        service = QbtService()
        self.assertEqual(service.list_torrents(), [torrent])
        self.assertIs(service.get_torrent("abc"), torrent)
        service.pause("abc")
        service.resume("abc")

        self.assertEqual(client_cls.call_count, 1)
        self.assertEqual(log_in.call_count, 1)
        client.torrents_pause.assert_called_once_with(torrent_hashes="abc")
        client.torrents_resume.assert_called_once_with(torrent_hashes="abc")

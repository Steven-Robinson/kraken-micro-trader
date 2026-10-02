"""Unit tests for Kraken Pro REST API Client."""

from decimal import Decimal
import unittest
from unittest.mock import patch, MagicMock

from kraken_client import KrakenClient, KrakenAPIError


class TestKrakenClient(unittest.TestCase):
    def setUp(self):
        self.client = KrakenClient(api_key="mock_key", api_secret="bW9ja19zZWNyZXQ=", dry_run=False)

    def test_strictly_monotonic_nonces_in_tight_loop(self):
        """Verify that _next_nonce always produces strictly increasing values even within the same millisecond."""
        nonces = [self.client._next_nonce() for _ in range(100)]
        for i in range(1, len(nonces)):
            self.assertGreater(
                nonces[i],
                nonces[i - 1],
                f"Nonce at index {i} ({nonces[i]}) was not strictly greater than previous ({nonces[i-1]})",
            )

    @patch("urllib.request.urlopen")
    def test_request_retries_on_http_502(self, mock_urlopen):
        """Verify _request retries transient HTTP 502/503/504 errors up to max_retries."""
        import urllib.error

        # First call raises HTTP 502, second call succeeds
        error_resp = urllib.error.HTTPError(
            url="https://api.kraken.com/0/public/Time",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=MagicMock(read=lambda: b"Bad Gateway"),
        )
        mock_success = MagicMock()
        mock_success.read.return_value = b'{"error":[],"result":{"unixtime":1700000000}}'
        mock_success.__enter__.return_value = mock_success
        mock_urlopen.side_effect = [error_resp, mock_success]

        res = self.client.get_server_time()
        self.assertEqual(res, 1700000000)
        self.assertEqual(mock_urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()

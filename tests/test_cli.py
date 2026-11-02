import sys
import unittest
from unittest import mock

import cli


class UnifiedCliTests(unittest.TestCase):
    def test_game_dispatch_and_offline_normalization(self) -> None:
        with mock.patch.object(sys, "argv", ["cli.py", "report", "--game", "eurojackpot", "--offline"]), \
             mock.patch("eurojackpot.main") as euro_main:
            cli.main()
            euro_main.assert_called_once()
            self.assertEqual(sys.argv, ["cli.py", "--offline", "report"])

    def test_default_game_is_lotto(self) -> None:
        with mock.patch.object(sys, "argv", ["cli.py", "report"]), mock.patch("app.main") as lotto_main:
            cli.main()
        lotto_main.assert_called_once()


if __name__ == "__main__":
    unittest.main()

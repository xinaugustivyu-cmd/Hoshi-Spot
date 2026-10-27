import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from hoshi_v11.safe_launcher import UnsafeConfiguration, main, prepare_environment, validate_configuration


ROOT = Path(__file__).resolve().parents[1]


class SafeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "user_data/config.json").read_text(encoding="utf-8"))
        self.config["strategy"] = "HoshiSpotV11A"
        self.env = {
            "FREQTRADE__DRY_RUN": "true",
            "FREQTRADE__API_SERVER__USERNAME": "hoshi-research",
            "FREQTRADE__API_SERVER__PASSWORD": "a-unique-local-passphrase-123",
            "FREQTRADE__API_SERVER__JWT_SECRET_KEY": "test-only-random-string-at-least-32-characters",
        }

    def test_clean_simulation_configuration_passes(self):
        validate_configuration(self.config, self.env, "A")
        cleaned = prepare_environment(self.env)
        self.assertEqual(cleaned["FREQTRADE__DRY_RUN"], "true")
        self.assertFalse(any("EXCHANGE__KEY" in key for key in cleaned))

    def test_dry_run_false_rejected_in_config_or_environment(self):
        config = deepcopy(self.config)
        config["dry_run"] = False
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(config, self.env, "A")
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(self.config, {**self.env, "FREQTRADE__DRY_RUN": "false"}, "A")

    def test_futures_margin_leverage_and_risk_relaxation_rejected(self):
        for key, value in (("trading_mode", "futures"), ("margin_mode", "isolated"),
                           ("leverage", 2), ("stoploss", -0.08)):
            with self.subTest(key=key):
                config = deepcopy(self.config)
                config[key] = value
                with self.assertRaises(UnsafeConfiguration):
                    validate_configuration(config, self.env, "A")
        config = deepcopy(self.config)
        config["strategy_parameters"]["max_position_fraction"] = 0.5
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(config, self.env, "A")

    def test_exchange_credentials_rejected_without_logging_values(self):
        config = deepcopy(self.config)
        config["exchange"]["key"] = "never-use-this"
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(config, self.env, "A")
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(self.config, {**self.env, "FREQTRADE__EXCHANGE__KEY": ""}, "A")

    def test_unknown_freqtrade_override_rejected(self):
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(self.config, {**self.env, "FREQTRADE__TRADING_MODE": "spot"}, "A")

    def test_public_default_webui_password_rejected(self):
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(self.config, {**self.env, "FREQTRADE__API_SERVER__PASSWORD": "password"}, "A")

    def test_three_account_identity_cannot_be_swapped(self):
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(self.config, self.env, "B")

    def test_prepared_account_configs_are_isolated_and_safe(self):
        database_urls = set()
        log_paths = set()
        for account in "ABC":
            config = json.loads((ROOT / f"user_data/v11_configs/{account}.json").read_text(encoding="utf-8"))
            validate_configuration(config, self.env, account)
            database_urls.add(config["db_url"])
            log_paths.add(config["logfile"])
            self.assertEqual(config["api_server"]["password"], "")
            self.assertEqual(config["api_server"]["jwt_secret_key"], "")
            self.assertEqual(config["initial_state"], "stopped")
        self.assertEqual(len(database_urls), 3)
        self.assertEqual(len(log_paths), 3)

    def test_compose_limits_ui_to_localhost_and_never_starts_automatically(self):
        source = (ROOT / "docker-compose.v11.yml").read_text(encoding="utf-8")
        for port in (8081, 8082, 8083):
            self.assertIn(f"127.0.0.1:{port}:8080", source)
        self.assertEqual(source.count('restart: "no"'), 1)
        self.assertIn('FREQTRADE__DRY_RUN: "true"', source)
        self.assertNotIn("FREQTRADE__EXCHANGE__KEY", source)

    def test_invalid_config_never_invokes_freqtrade_process(self):
        config = deepcopy(self.config)
        config["dry_run"] = False
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "unsafe.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch("hoshi_v11.safe_launcher.os.environ", self.env), patch(
                "hoshi_v11.safe_launcher.subprocess.call"
            ) as launch:
                with self.assertRaises(UnsafeConfiguration):
                    main(["A", str(path)])
                launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

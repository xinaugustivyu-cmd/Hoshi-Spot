import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import yaml

from hoshi_v11.safe_launcher import UnsafeConfiguration, main, prepare_environment, validate_configuration
from hoshi_v11.guarded_freqtrade import validate_trade_argv


ROOT = Path(__file__).resolve().parents[1]


class SafeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "user_data/v11_configs/A.json").read_text(encoding="utf-8"))
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

    def test_config_cannot_relax_other_risk_or_startup_gates(self):
        for key, value in (("max_drawdown_limit", 0.99),
                           ("fee_per_side", 0),
                           ("stoploss_guard_stop_candles", 0)):
            with self.subTest(parameter=key):
                config = deepcopy(self.config)
                config["strategy_parameters"][key] = value
                with self.assertRaises(UnsafeConfiguration):
                    validate_configuration(config, self.env, "A")
        for key, value in (("initial_state", "running"),
                           ("force_entry_enable", True),
                           ("use_exit_signal", False),
                           ("process_only_new_candles", False),
                           ("position_adjustment_enable", True)):
            with self.subTest(option=key):
                config = deepcopy(self.config)
                config[key] = value
                with self.assertRaises(UnsafeConfiguration):
                    validate_configuration(config, self.env, "A")

    def test_exchange_credentials_rejected_without_logging_values(self):
        config = deepcopy(self.config)
        config["exchange"]["key"] = "never-use-this"
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(config, self.env, "A")
        config = deepcopy(self.config)
        config["exchange"]["password"] = "never-use-this"
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(config, self.env, "A")
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(self.config, {**self.env, "FREQTRADE__EXCHANGE__KEY": ""}, "A")

    def test_unknown_freqtrade_override_rejected(self):
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(self.config, {**self.env, "FREQTRADE__TRADING_MODE": "spot"}, "A")
        with self.assertRaises(UnsafeConfiguration):
            validate_configuration(self.config, {**self.env, "PYTHONPATH": "/tmp/bypass"}, "A")

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
        compose = yaml.safe_load(source)
        for port in (8081, 8082, 8083):
            self.assertIn(f"127.0.0.1:{port}:8080", source)
        self.assertEqual(source.count('restart: "no"'), 3)
        self.assertIn('FREQTRADE__DRY_RUN: "true"', source)
        self.assertNotIn("FREQTRADE__EXCHANGE__KEY", source)
        self.assertIn("hoshi-paper-only:\n    internal: true", source)
        self.assertEqual(source.count('profiles: ["future-live"]'), 2)
        self.assertEqual(source.count('hoshi-v11-market-events:/freqtrade/user_data/v11_market:ro'), 3)
        self.assertTrue(compose["networks"]["hoshi-paper-only"]["internal"])
        self.assertEqual(set(compose["volumes"]), {
            "hoshi-v11-a-account", "hoshi-v11-b-account", "hoshi-v11-c-account",
            "hoshi-v11-market-events",
        })
        for account in "abc":
            service = compose["services"][f"hoshi-{account}"]
            self.assertEqual(service["networks"], ["hoshi-paper-only"])
            self.assertEqual(service["profiles"], ["future-live"])
            self.assertIn(f"hoshi-v11-{account}-account:/freqtrade/user_data/v11_accounts/{account.upper()}",
                          service["volumes"])
            self.assertNotIn(f"./user_data/v11_accounts/{account.upper()}:/freqtrade/user_data/v11_accounts/{account.upper()}",
                             service["volumes"])
        self.assertEqual(set(compose["services"]["hoshi-market-ingest"]["networks"]),
                         {"hoshi-paper-only", "hoshi-market-egress"})
        self.assertIn("hoshi-v11-market-events:/market",
                      compose["services"]["hoshi-market-ingest"]["volumes"])
        initializer = compose["services"]["hoshi-volume-init"]
        self.assertEqual(initializer["profiles"], ["manual-setup"])
        self.assertEqual(initializer["network_mode"], "none")
        self.assertEqual(initializer["entrypoint"][0], "chown")
        self.assertEqual(set(initializer["volumes"]), {
            "hoshi-v11-a-account:/a", "hoshi-v11-b-account:/b",
            "hoshi-v11-c-account:/c", "hoshi-v11-market-events:/market",
        })

    def test_launcher_uses_guarded_entry_and_strips_unrelated_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "safe.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            with patch("hoshi_v11.safe_launcher.os.environ", {
                **self.env, "PYTHONPATH": "/freqtrade/user_data", "HTTP_PROXY": "http://unsafe",
            }), patch("hoshi_v11.safe_launcher.subprocess.call", return_value=0) as launch:
                self.assertEqual(main(["A", str(path)]), 0)
                command = launch.call_args.args[0]
                child_env = launch.call_args.kwargs["env"]
                self.assertEqual(command[1:3], ["-m", "hoshi_v11.guarded_freqtrade"])
                self.assertEqual(child_env["FREQTRADE__DRY_RUN"], "true")
                self.assertEqual(child_env["PYTHONPATH"], "/freqtrade/user_data")
                self.assertNotIn("HTTP_PROXY", child_env)

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

    def test_direct_guarded_entry_rejects_cli_bypass(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "safe.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            argv = ["trade", "--config", str(path), "--strategy", "HoshiSpotV11A", "--dry-run"]
            validate_trade_argv(argv, self.env)
            with self.assertRaises(UnsafeConfiguration):
                validate_trade_argv(argv + ["--dry-run-wallet", "100000"], self.env)
            with self.assertRaises(UnsafeConfiguration):
                validate_trade_argv(argv, {**self.env, "FREQTRADE__STOPLOSS": "-1"})


if __name__ == "__main__":
    unittest.main()

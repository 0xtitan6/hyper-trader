from src.alerts import NullAlerter, WebhookAlerter
from src.main import build_alerter, install_signal_handler, parse_args


def test_parse_args_defaults():
    ns = parse_args([])
    assert ns.config == "config.yaml"
    assert ns.preflight is False
    assert ns.skip_preflight is False


def test_parse_args_preflight_flag():
    ns = parse_args(["--preflight"])
    assert ns.preflight is True


def test_parse_args_custom_config():
    ns = parse_args(["--config", "foo.yaml"])
    assert ns.config == "foo.yaml"


def test_parse_args_skip_preflight():
    ns = parse_args(["--skip-preflight"])
    assert ns.skip_preflight is True


def test_build_alerter_null_when_no_url(cfg):
    alerter = build_alerter(cfg)
    assert isinstance(alerter, NullAlerter)


def test_build_alerter_webhook_when_url_set(cfg):
    from dataclasses import replace

    cfg2 = replace(cfg, webhook_url="https://hooks.slack.example/x")
    alerter = build_alerter(cfg2)
    assert isinstance(alerter, WebhookAlerter)


def test_install_signal_handler_returns_unset_event():
    e = install_signal_handler()
    assert not e.is_set()

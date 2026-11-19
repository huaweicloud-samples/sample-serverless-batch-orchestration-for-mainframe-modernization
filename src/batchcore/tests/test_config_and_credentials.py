"""Configuration, identity and logging: the layer every handler stands on."""

from batchcore.shared import config, credentials, logs


def test_values_are_read_when_a_call_is_made_not_when_the_module_is_loaded(env):
    env.setenv(config.OBS_BUCKET, "named-then-changed")
    assert config.bucket() == "named-then-changed"
    env.setenv(config.OBS_BUCKET, "changed-later")
    assert config.bucket() == "changed-later"


def test_an_empty_value_falls_back_to_the_default(env):
    env.setenv(config.MAX_AGE_DAYS, "   ")
    assert config.max_age_days() == 2
    env.setenv(config.MAX_AGE_DAYS, "not a number")
    assert config.max_age_days() == 2
    env.setenv(config.MAX_AGE_DAYS, "5")
    assert config.max_age_days() == 5


def test_the_deployment_region_has_a_stated_default(env):
    env.delenv(config.REGION)
    assert config.region() == "cn-north-4"
    assert config.obs_endpoint() == "obs.cn-north-4.myhuaweicloud.com"


def test_a_prefix_always_ends_with_one_slash(env):
    env.setenv(config.INBOUND_PREFIX, "inbound/day")
    assert config.inbound_prefix() == "inbound/day/"
    assert config.runs_prefix().endswith("/")


def test_a_flag_accepts_the_shape_a_deployment_writes(env):
    for value in ("1", "true", "YES", "on"):
        env.setenv(config.ENABLE_CUSTOM_METRICS, value)
        assert config.flag(config.ENABLE_CUSTOM_METRICS, False) is True, value
    for value in ("0", "false", "off", "anything else"):
        env.setenv(config.ENABLE_CUSTOM_METRICS, value)
        assert config.flag(config.ENABLE_CUSTOM_METRICS, True) is False, value


def test_an_isolated_run_moves_every_prefix_of_that_run(env):
    assert config.isolation_prefix("") == ""
    assert config.isolation_prefix(None) == ""
    assert config.isolation_prefix("night-7") == "runs/night-7/"
    assert config.isolation_prefix("/night-7/") == "runs/night-7/"


def test_the_runtime_triple_is_the_only_identity_used_in_production():
    class Context:
        def getSecurityAccessKey(self):
            return "AK"

        def getSecuritySecretKey(self):
            return "SK"

        def getSecurityToken(self):
            return None

    assert credentials.from_context(Context()) == ("AK", "SK", None)
    assert credentials.from_context(None) is None


def test_a_context_that_handed_over_nothing_is_not_an_identity():
    class Empty:
        def getSecurityAccessKey(self):
            return ""

        def getSecuritySecretKey(self):
            return ""

        def getSecurityToken(self):
            return ""

    assert credentials.from_context(Empty()) is None
    assert credentials.resolve(Empty()) is None


def test_an_environment_profile_is_read_only_when_the_runtime_gave_nothing(env):
    assert credentials.resolve(None) is None
    env.setenv(credentials.ACCESS_KEY, "AK")
    env.setenv(credentials.SECRET_KEY, "SK")
    env.setenv(credentials.SECURITY_TOKEN, "TOKEN")
    assert credentials.resolve(None) == ("AK", "SK", "TOKEN")
    env.setenv(credentials.SECURITY_TOKEN, "")
    assert credentials.resolve(None) == ("AK", "SK", None)
    env.delenv(credentials.SECRET_KEY)
    assert credentials.resolve(None) is None


def test_a_failure_message_never_carries_a_key():
    described = credentials.describe(("AKIDEXAMPLESECRET", "SK", "TOKEN"))
    assert described == "access key ending CRET"
    assert "SK" not in described
    assert credentials.describe(None) == (
        "no credentials: the agency was not assumed and %s is unset" % credentials.ACCESS_KEY
    )


def test_the_project_of_a_call_comes_from_the_runtime_first(env):
    class Context:
        def getProjectId(self):
            return "project-from-context"

    assert credentials.project(Context()) == "project-from-context"
    assert credentials.project(None) == "project-for-tests"
    env.delenv(config.PROJECT_ID)
    assert credentials.project(None) == ""


def test_a_logger_is_a_child_of_the_one_configured_stream(env):
    log = logs.get("orchestrator")
    assert log.name == "batch.orchestrator"
    logs.entry("a handler")
    assert logging_root_configured() is True


def logging_root_configured():
    import logging

    return bool(logging.getLogger().handlers)

"""分别加载应用、同步、模型配置。

各加载器只取所属字段，不建立配置注册中心；
Pydantic ``SecretStr`` 隐藏 DSN 和密钥，``repr`` 不泄露凭证。
"""

from __future__ import annotations

from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

Environment = Literal["development", "production"]


class AppSettings(BaseModel):
    """页面应用配置：只读身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reader_dsn: SecretStr
    shop_ids: frozenset[str]
    environment: Environment
    allowed_subjects: frozenset[str]


class SyncSettings(BaseModel):
    """同步命令配置：写入身份与快麦凭证。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    writer_dsn: SecretStr
    shop_ids: frozenset[str]
    app_key: SecretStr
    app_secret: SecretStr
    access_token: SecretStr
    refresh_token: SecretStr


class ModelSettings(BaseModel):
    """模型配置：只使用所选 provider 的密钥。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["qwen", "deepseek"]
    model: str
    api_key: SecretStr
    base_url: str | None = None


def _required(env: Mapping[str, str], key: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ValueError(f"缺少 {key}")
    return value


def _shop_ids(env: Mapping[str, str]) -> frozenset[str]:
    raw = (env.get("BI_SHOP_IDS") or "").strip()
    if not raw:
        raise ValueError("缺少 BI_SHOP_IDS")
    shops = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if not shops:
        raise ValueError("BI_SHOP_IDS 不能为空集合")
    return shops


def load_app_settings(env: Mapping[str, str]) -> AppSettings:
    """加载页面应用配置；应用环境不得包含写入 DSN。"""
    environment = (_required(env, "APP_ENV") or "development").strip()
    if environment not in ("development", "production"):
        raise ValueError("APP_ENV 只能是 development 或 production")
    allowed = frozenset(
        part.strip()
        for part in (env.get("APP_ALLOWED_SUBJECTS") or "").split(",")
        if part.strip()
    )
    if environment == "production" and not allowed:
        raise ValueError("生产环境必须配置 APP_ALLOWED_SUBJECTS")
    return AppSettings(
        reader_dsn=SecretStr(_required(env, "BI_READER_DSN")),
        shop_ids=_shop_ids(env),
        environment=environment,  # type: ignore[arg-type]
        allowed_subjects=allowed,
    )


def load_sync_settings(env: Mapping[str, str]) -> SyncSettings:
    """加载同步配置：写入 DSN 与快麦四个凭证字段。"""
    return SyncSettings(
        writer_dsn=SecretStr(_required(env, "BI_WRITER_DSN")),
        shop_ids=_shop_ids(env),
        app_key=SecretStr(_required(env, "KUAI_MAI_APP_KEY")),
        app_secret=SecretStr(_required(env, "KUAI_MAI_APP_SECRET")),
        access_token=SecretStr(_required(env, "KUAI_MAI_ACCESS_TOKEN")),
        refresh_token=SecretStr(_required(env, "KUAI_MAI_REFRESH_TOKEN")),
    )


def load_model_settings(env: Mapping[str, str]) -> ModelSettings:
    """加载模型配置；只读取所选 provider 自己的密钥。"""
    provider = _required(env, "LLM_PROVIDER")
    key_name = {"qwen": "QWEN_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}.get(provider)
    if key_name is None:
        raise ValueError("不支持的模型 provider")
    api_key = (env.get(key_name) or "").strip()
    if not api_key:
        raise ValueError(f"缺少 {key_name}")
    model = _required(env, "LLM_MODEL")
    base_url_raw = (env.get("LLM_BASE_URL") or "").strip() or None
    if base_url_raw is not None and not base_url_raw.lower().startswith("https://"):
        raise ValueError("LLM_BASE_URL 只允许 HTTPS 地址")
    return ModelSettings(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url_raw,
    )

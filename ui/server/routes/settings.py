"""Settings: automation schedule, trading window and auto-send, research (LLM) settings, and API keys.

LLM settings and keys are written to the repo's .env with python-dotenv, which keeps every other line as
it is, and copied into this process's environment so the next script or research run sees them without a
restart (a child process inherits this environment, and load_dotenv never overrides a variable already set).
Key values are write-only: the API says whether each key is set, never what it is.
"""

from __future__ import annotations

import os
import re

from dotenv import dotenv_values, set_key, unset_key
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from extensions.market_data.alpaca_bars import API_KEY_ENV, PAPER_KEY_PREFIX, SECRET_KEY_ENV
from paper_orders.trade_window import SettingsError
from ui.server import broker, paths, settings
from ui.server.scheduler import scheduler

router = APIRouter(prefix="/api/settings", tags=["settings"])

# tradingagents/llm_clients/api_key_env.py PROVIDER_API_KEY_ENV, copied so this server need not import
# the tradingagents package (which loads LangChain).
PROVIDERS = {
    "google": "GOOGLE_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "xai": "XAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY", "qwen": "DASHSCOPE_API_KEY", "qwen-cn": "DASHSCOPE_CN_API_KEY",
    "glm": "ZHIPU_API_KEY", "glm-cn": "ZHIPU_CN_API_KEY", "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY", "mistral": "MISTRAL_API_KEY", "kimi": "MOONSHOT_API_KEY",
    "groq": "GROQ_API_KEY", "nvidia": "NVIDIA_API_KEY", "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    "bedrock": None, "ollama": None,
}

EFFORTS = ["", "low", "medium", "high"]
LLM_FIELDS: dict[str, dict] = {
    "TRADINGAGENTS_LLM_PROVIDER": {"label": "LLM provider", "choices": list(PROVIDERS)},
    "TRADINGAGENTS_DEEP_THINK_LLM": {"label": "Deep-thinking model", "pattern": r"[\w.:/\-]{1,120}"},
    "TRADINGAGENTS_QUICK_THINK_LLM": {"label": "Quick-thinking model", "pattern": r"[\w.:/\-]{1,120}"},
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS": {"label": "Bull/bear debate rounds", "int": (1, 5)},
    "TRADINGAGENTS_MAX_RISK_ROUNDS": {"label": "Risk debate rounds", "int": (1, 5)},
    "TRADINGAGENTS_OUTPUT_LANGUAGE": {"label": "Report language", "pattern": r"[A-Za-z][A-Za-z \-]{0,39}"},
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL": {"label": "Google thinking level",
                                            "choices": ["", "minimal", "low", "medium", "high"]},
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": {"label": "OpenAI reasoning effort", "choices": EFFORTS},
    "TRADINGAGENTS_ANTHROPIC_EFFORT": {"label": "Anthropic effort", "choices": EFFORTS},
}

KEYS: dict[str, str] = {
    API_KEY_ENV: "Alpaca paper key ID",
    SECRET_KEY_ENV: "Alpaca paper secret key",
    **{env: f"{provider} API key" for provider, env in PROVIDERS.items() if env},
}
_SECRET = re.compile(r"^[!-~]{1,400}$")  # printable ASCII, no spaces or line breaks


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _env_file() -> dict[str, str | None]:
    path = paths.env_path()
    return dotenv_values(path) if path.exists() else {}


def _write_env(name: str, value: str | None) -> None:
    """Set or (value None/"") remove one variable in .env and in this process's environment."""
    path = paths.env_path()
    if value:
        path.touch(exist_ok=True)
        set_key(str(path), name, value, quote_mode="auto")
        os.environ[name] = value
    else:
        if path.exists() and name in dotenv_values(path):
            unset_key(str(path), name, quote_mode="auto")
        os.environ.pop(name, None)


@router.get("")
def get_settings() -> dict:
    env = _env_file()
    try:
        automation, trading = settings.automation(), settings.trading()
        saved_trading = settings.trading(saved_only=True)
        error = None
    except SettingsError as exc:
        automation, trading, saved_trading, error = settings.DEFAULT_AUTOMATION, settings.DEFAULT_TRADING, None, str(exc)
    return {
        "error": error,
        "automation": automation,
        "automation_runs": settings.AUTOMATION_RUNS,
        "trading": trading,
        "trading_saved": saved_trading is not None,
        "llm": {name: {**spec, "value": env.get(name) or os.environ.get(name) or ""} for name, spec in LLM_FIELDS.items()},
        "keys": [{"name": name, "label": label, "set": bool(env.get(name) or os.environ.get(name))}
                 for name, label in KEYS.items()],
        "provider_keys": PROVIDERS,
        "scheduler": scheduler.status(),
        "env_path": str(paths.env_path()),
        "settings_path": str(paths.settings_path()),
    }


@router.get("/scheduler")
def scheduler_status() -> dict:
    return scheduler.status()


@router.put("/automation")
def put_automation(body: dict) -> dict:
    try:
        return settings.save_section("automation", settings.validate_automation(body))
    except SettingsError as exc:
        raise _bad(str(exc)) from None


@router.put("/trading")
def put_trading(body: dict) -> dict:
    try:
        return settings.save_section("trading", settings.validate_trading(body))
    except SettingsError as exc:
        raise _bad(str(exc)) from None


class EnvValues(BaseModel):
    values: dict[str, str]


@router.put("/llm")
def put_llm(body: EnvValues) -> dict:
    for name, raw in body.values.items():
        spec = LLM_FIELDS.get(name)
        if spec is None:
            raise _bad(f"{name} is not an LLM setting")
        value = raw.strip()
        if not value:
            continue
        if "choices" in spec and value not in spec["choices"]:
            raise _bad(f"{spec['label']} must be one of {', '.join(c for c in spec['choices'] if c)}")
        if "int" in spec:
            lo, hi = spec["int"]
            if not value.isdigit() or not lo <= int(value) <= hi:
                raise _bad(f"{spec['label']} must be a whole number from {lo} to {hi}")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
            raise _bad(f"{spec['label']} {value!r} has characters that are not allowed")
    for name, raw in body.values.items():
        _write_env(name, raw.strip())
    return {"ok": True}


class KeyUpdate(BaseModel):
    set: dict[str, str] = {}
    clear: list[str] = []


@router.put("/keys")
def put_keys(body: KeyUpdate) -> dict:
    """Write-only: values are checked and saved, never echoed back. A blank value leaves the key as it is."""
    updates = {k: v.strip() for k, v in body.set.items() if v.strip()}
    for name in [*updates, *body.clear]:
        if name not in KEYS:
            raise _bad(f"{name} is not a key this page manages")
    for name, value in updates.items():
        if not _SECRET.match(value):
            raise _bad(f"{KEYS[name]}: spaces, line breaks and non-ASCII characters are not allowed")
    if API_KEY_ENV in updates and not updates[API_KEY_ENV].startswith(PAPER_KEY_PREFIX):
        raise _bad(f"{KEYS[API_KEY_ENV]} must be a paper key (paper key IDs start with {PAPER_KEY_PREFIX!r})")
    for name, value in updates.items():
        _write_env(name, value)
    for name in body.clear:
        _write_env(name, None)
    if {API_KEY_ENV, SECRET_KEY_ENV} & {*updates, *body.clear}:
        broker.reset()  # the next account read builds a client from the new keys
    return {"ok": True, "saved": sorted(updates), "cleared": sorted(body.clear)}

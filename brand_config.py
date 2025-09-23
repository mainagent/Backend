# brand_config.py
import os

def env_for(brand: str, base: str, default: str | None = None) -> str | None:
    """
    Resolve BRANDED env var first (e.g. EMAIL_FROM_NAME_HAIR),
    else fallback to unscoped (EMAIL_FROM_NAME), else `default`.
    """
    key = f"{base}_{brand.upper()}"
    return os.getenv(key) or os.getenv(base) or default
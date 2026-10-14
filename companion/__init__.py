import logging

_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")


def _setup_logging():
    from .config import LOG_DIR

    LOG_DIR.mkdir(exist_ok=True)
    _h = logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")
    _h.setFormatter(_fmt)
    _h_err = logging.StreamHandler()
    _h_err.setFormatter(_fmt)
    logging.basicConfig(level=logging.INFO, handlers=[_h, _h_err])
    # Hermes の /mcp キープアライブ ping と stateless セッション終了の
    # INFO ノイズ、および httpx の HTTP Request 明細をログから落とす
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


_setup_logging()

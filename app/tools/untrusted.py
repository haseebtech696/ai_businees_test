"""Anything scraped from the web is untrusted DATA. Wrap it so the model can tell data from instructions."""
from html import escape

TAG = "untrusted_web_content"


def wrap_untrusted(text: str, source: str = "") -> str:
    # Neutralise any tag the page itself contains so scraped text cannot close the wrapper early.
    safe = text.replace(f"</{TAG}", f"[/{TAG}").replace(f"<{TAG}", f"[{TAG}")
    attr = f' source="{escape(source, quote=True)}"' if source else ""
    return f"<{TAG}{attr}>\n{safe}\n</{TAG}>"

from __future__ import annotations

import asyncio

from nika_core.workspaces.semantic_adapters import PlaywrightSemanticAdapter


async def main() -> None:
    adapter = PlaywrightSemanticAdapter()
    evidence = await adapter.inspect_browser(
        "html:<main><h1>Accessibility repair</h1>"
        "<label for='problem'>Problem description</label>"
        "<input id='problem'><button>Repair now</button></main>"
    )
    assert evidence.method.value == "dom"
    assert 'heading "Accessibility repair"' in evidence.summary
    assert 'textbox "Problem description"' in evidence.summary
    assert 'button "Repair now"' in evidence.summary
    assert evidence.accessible_controls


if __name__ == "__main__":
    asyncio.run(main())

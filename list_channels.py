"""List the channel slots configured on the companion radio.

Stop the bot first — it holds the serial port. Port comes from MESHCORE_PORT or settings.yaml.
"""
import asyncio
import os

import yaml
from meshcore import MeshCore


def _port() -> str:
    with open("settings.yaml", encoding="utf-8") as f:
        return os.getenv("MESHCORE_PORT") or yaml.safe_load(f)["meshcore"]["port"]


async def main():
    mc = await MeshCore.create_serial(_port(), 115200)
    try:
        for idx in range(8):
            try:
                result = await mc.commands.get_channel(idx)
                print(f"  slot {idx}: payload={result.payload!r}  type={result.type}")
            except Exception as e:
                print(f"  slot {idx}: error — {e}")
    finally:
        await mc.disconnect()

asyncio.run(main())

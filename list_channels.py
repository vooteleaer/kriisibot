import asyncio
from meshcore import MeshCore

async def main():
    mc = await MeshCore.create_serial("COM59", 115200)
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

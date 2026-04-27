import asyncio
import json
import sys
from pathlib import Path

import aiofiles


async def split_by_ticker(input_path: Path, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    current_ticker: str | None = None
    out_file = None

    async with aiofiles.open(input_path) as src:
        async for raw_line in src:
            line = raw_line.strip()
            if not line:
                continue

            event = json.loads(line)
            ticker = event.get("market_ticker")

            if ticker and ticker != current_ticker:
                if out_file is not None:
                    await out_file.close()
                current_ticker = ticker
                dest = output_dir / f"{ticker}.jsonl"
                out_file = await aiofiles.open(dest, "a")
                print(f"-> {dest}")

            if out_file is not None:
                await out_file.write(raw_line if raw_line.endswith("\n") else line + "\n")

    if out_file is not None:
        await out_file.close()

    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        src_path = Path(sys.argv[1])
        dst_dir = Path(sys.argv[2])
    else:
        src_path = Path("data/events.jsonl")
        dst_dir = Path("data/markets")

    asyncio.run(split_by_ticker(src_path, dst_dir))

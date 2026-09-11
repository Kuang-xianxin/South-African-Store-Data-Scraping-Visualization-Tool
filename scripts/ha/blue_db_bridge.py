"""Laptop loopback BLUE SQL transport: prefer company LAN, then authenticated tailnet.

Only forwards bytes. MySQL clients independently verify the dedicated BLUE CA,
hostname, server id/uuid and seed; this listener never connects to green ports.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import blue_node


PORT = 13317
DESTINATIONS = ("192.168.110.180", "100.70.103.11")


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    remote_writer = None
    tasks = []
    try:
        for host in DESTINATIONS:
            try:
                remote_reader, remote_writer = await asyncio.wait_for(
                    asyncio.open_connection(host, PORT), timeout=1 if host == DESTINATIONS[0] else 8)
                break
            except (OSError, TimeoutError):
                continue
        if remote_writer is None:
            return
        tasks = [asyncio.create_task(relay(reader, remote_writer)),
                 asyncio.create_task(relay(remote_reader, writer))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        if remote_writer is not None:
            remote_writer.close()
        await asyncio.gather(writer.wait_closed(),
                             *([remote_writer.wait_closed()] if remote_writer else []),
                             return_exceptions=True)


async def main() -> None:
    if blue_node.config()["node"] != "laptop":
        raise RuntimeError("BLUE DB bridge is laptop-only")
    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    Path("D:/TakealotBlue/state/db-bridge.json").write_text(json.dumps({
        "listen": f"127.0.0.1:{PORT}", "destinations": DESTINATIONS, "port": PORT,
        "mode": "lan-preferred-tailnet-fallback",
    }), encoding="utf-8")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

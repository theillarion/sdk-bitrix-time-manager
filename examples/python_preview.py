"""Run from the project directory: python examples/python_preview.py."""

import asyncio
import json

from bitrix_time_manager import BitrixClient, RemoteWorkCreate, Settings, TimeManager


async def main() -> None:
    """Preview a remote day without creating anything on the portal."""
    settings = Settings.from_env(allow_writes=False)
    async with BitrixClient(settings) as client:
        manager = TimeManager(client)
        await manager.check_connection()
        result = await manager.create_remote_work(
            RemoteWorkCreate.model_validate(
                {
                    "day": "2026-10-19",
                    "start": "09:00",
                    "end": "18:00",
                    "comment": "Работа над проектом",
                }
            ),
            dry_run=True,
            idempotency_key="manual:remote:2026-10-19",
        )
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

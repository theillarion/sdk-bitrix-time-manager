"""Manual inspection and preview-first commands."""

import argparse
import asyncio
import json
import sys
from datetime import date, time
from typing import Any

from pydantic import ValidationError

from bitrix_time_manager import (
    BitrixClient,
    ContractRef,
    RemoteWorkCreate,
    Settings,
    TimeEntryCreate,
    TimeManager,
    WorkdayCreate,
    __version__,
)
from bitrix_time_manager.errors import TimeManagerError, redact
from bitrix_time_manager.journal import OperationJournal


def parser() -> argparse.ArgumentParser:
    """Build the CLI with explicit --apply for every mutating command."""
    root = argparse.ArgumentParser(prog="bitrix-time", description="Bitrix Time-manager: reads and create previews.")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--env-file", default=".env", help="Settings file, defaults to .env")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Check webhook and schema without writes")
    for name, help_text in (
        ("day", "Show a workday"),
        ("entries", "Time entries for a day"),
        ("remote-list", "Remote work for a day"),
    ):
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument("--date", type=date.fromisoformat, required=True)
    contracts = commands.add_parser("contracts", help="Find a contract or template by title")
    contracts.add_argument("query")
    contracts.add_argument("--type", type=int, choices=(1048, 1052, 1298), default=1298)
    contracts.add_argument("--limit", type=int, default=20)
    for name in ("workday", "remote", "entry"):
        cmd = commands.add_parser(name, help=f"Create {name}; without --apply this is a preview only")
        cmd.add_argument("--date", type=date.fromisoformat, required=True)
        cmd.add_argument("--apply", action="store_true", help="Send one create operation to Bitrix")
        cmd.add_argument("--idempotency-key")
        if name == "entry":
            cmd.add_argument("--contract-type", type=int, choices=(1048, 1052, 1298), required=True)
            cmd.add_argument("--contract-id", type=int, required=True)
            cmd.add_argument("--description", required=True)
            cmd.add_argument("--hours", type=int, required=True)
            cmd.add_argument("--minutes", type=int, choices=(0, 30), default=0)
        else:
            cmd.add_argument("--from", dest="start", type=time.fromisoformat, required=True)
            cmd.add_argument("--to", dest="end", type=time.fromisoformat, required=True)
            cmd.add_argument("--comment", default="")
    commands.add_parser("journal", help="Local operation journal states")
    resolve = commands.add_parser("resolve", help="Allow a retry after a manual no-record check")
    resolve.add_argument("operation_id")
    resolve.add_argument("--confirmed-not-created", action="store_true", required=True)
    return root


async def run(args: argparse.Namespace) -> Any:
    """Dispatch one command through the reusable service."""
    settings = Settings.from_env(args.env_file, allow_writes=bool(getattr(args, "apply", False)))
    journal = OperationJournal(settings.state_path)
    if args.command == "journal":
        return journal.list_operations()
    if args.command == "resolve":
        journal.confirm_not_created(args.operation_id)
        return {"operation_id": args.operation_id, "state": "failed", "note": "Retry allowed; nothing was written."}
    async with BitrixClient(settings) as client:
        service = TimeManager(client, journal)
        if args.command == "check":
            return await service.check_connection()
        if args.command == "day":
            result = await service.get_workday(args.date)
            return result.model_dump(mode="json") if result else None
        if args.command == "entries":
            return await service.list_time_entries(args.date)
        if args.command == "remote-list":
            return await service.list_remote_work(args.date)
        if args.command == "contracts":
            return [
                item.model_dump(mode="json")
                for item in await service.search_contracts(args.query, entity_type_id=args.type, limit=args.limit)
            ]
        options = {"dry_run": not args.apply, "idempotency_key": args.idempotency_key}
        if args.command == "entry":
            spec = TimeEntryCreate.model_validate(
                {
                    "day": args.date,
                    "contract": ContractRef.model_validate(
                        {
                            "entity_type_id": args.contract_type,
                            "item_id": args.contract_id,
                        }
                    ),
                    "description": args.description,
                    "hours": args.hours,
                    "minutes": args.minutes,
                }
            )
            outcome = await service.create_time_entry(spec, **options)
        else:
            interval = {"day": args.date, "start": args.start, "end": args.end, "comment": args.comment}
            if args.command == "remote":
                outcome = await service.create_remote_work(RemoteWorkCreate.model_validate(interval), **options)
            else:
                outcome = await service.create_workday(WorkdayCreate.model_validate(interval), **options)
        return outcome.model_dump(mode="json")


def main() -> int:
    """Print JSON for success and a sanitized error for failure."""
    args = parser().parse_args()
    try:
        result = asyncio.run(run(args))
    except (TimeManagerError, ValidationError, ValueError, OSError) as error:
        print(
            json.dumps(
                {"error": getattr(error, "code", "invalid_request"), "detail": redact(str(error))}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print("Operation interrupted; check the journal before retrying a send.", file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

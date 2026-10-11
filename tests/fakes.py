"""A stateful mock of the documented Bitrix REST boundary."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from bitrix_time_manager.config import PortalSchema


@dataclass
class Portal:
    items: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    lose_add_response: bool = False
    reject_add: bool = False
    malformed_add: bool = False
    schema_drift: bool = False

    def fields(self, type_id: int) -> dict[str, Any]:
        s = PortalSchema()
        definitions = {
            1208: {
                s.day_date: "date",
                s.day_start: "datetime",
                s.day_end: "datetime",
                s.day_employee: "employee",
                s.day_comment: "string",
                s.day_source: "enumeration",
                s.day_kind: "iblock_element",
            },
            1218: {
                s.time_contract: "crm",
                s.time_description: "string",
                s.time_hours: "double",
                s.time_minutes: "enumeration",
                "parentId1208": "crm_entity",
            },
            1250: {s.remote_start: "datetime", s.remote_end: "datetime", s.remote_comment: "string"},
        }
        result: dict[str, Any] = {
            key: {"type": value, "isReadOnly": False} for key, value in definitions[type_id].items()
        }
        if type_id == 1218:
            result[s.time_minutes]["items"] = [{"ID": "193", "VALUE": "0"}, {"ID": "194", "VALUE": "30"}]
            result[s.time_contract]["settings"] = {"DYNAMIC_1048": "Y", "DYNAMIC_1052": "Y", "DYNAMIC_1298": "Y"}
        if self.schema_drift:
            result.pop(next(iter(result)))
        return result

    @staticmethod
    def matches(item: dict[str, Any], filters: dict[str, Any]) -> bool:
        for key, expected in filters.items():
            op = next((prefix for prefix in (">=", "<=", ">", "<", "%") if key.startswith(prefix)), "=")
            name = key if op == "=" else key[len(op) :]
            actual = item.get(name)
            if actual is None:
                return False
            if op == "%":
                match = str(expected).casefold() in str(actual).casefold()
            elif op == ">=":
                match = str(actual) >= str(expected)
            elif op == "<=":
                match = str(actual) <= str(expected)
            elif op == ">":
                match = str(actual) > str(expected)
            elif op == "<":
                match = str(actual) < str(expected)
            else:
                match = (
                    str(actual)[:10] == expected
                    if isinstance(expected, str) and len(expected) == 10
                    else str(actual) == str(expected)
                )
            if not match:
                return False
        return True

    def respond(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        payload = json.loads(request.content)
        self.calls.append((method, payload))
        type_id = payload.get("entityTypeId", 0)
        if method == "profile":
            result: Any = {"ID": "500"}
        elif method == "method.get":
            result = {"isExisting": True, "isAvailable": True}
        elif method == "crm.item.fields":
            result = {"fields": self.fields(type_id)}
        elif method == "crm.item.list":
            rows = [item for item in self.items.get(type_id, []) if self.matches(item, payload["filter"])]
            selected = payload["select"]
            if selected != ["*"]:
                rows = [{key: value for key, value in row.items() if key in selected} for row in rows]
            result = {"items": rows}
        elif method == "crm.item.get":
            rows = [item for item in self.items.get(type_id, []) if item["id"] == payload["id"]]
            if not rows:
                return httpx.Response(400, json={"error": "NOT_FOUND", "error_description": "Item not found"})
            result = {"item": rows[0]}
        elif method == "crm.item.add":
            if self.reject_add:
                return httpx.Response(400, json={"error": "ACCESS_DENIED", "error_description": "Permission denied"})
            item = {"id": 9000 + len(self.items.get(type_id, [])), "title": "Created", **payload["fields"]}
            self.items.setdefault(type_id, []).append(item)
            if self.lose_add_response:
                raise httpx.ReadTimeout("Lost response", request=request)
            result = None if self.malformed_add else {"item": item}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"result": result})

    @property
    def adds(self) -> list[dict[str, Any]]:
        return [payload for method, payload in self.calls if method == "crm.item.add"]
